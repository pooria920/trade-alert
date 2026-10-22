import { createBot } from './bot/bot.js';
import { parseEnv } from './config/env.js';
import { openDatabase } from './database/client.js';
import {
  removeHeartbeat,
  writeHeartbeat,
  runtimeHealth,
} from './shared/health.js';
import { acquireInstanceLock } from './database/instance-lock.js';
import { applicationState } from './database/schema.js';
import { createLogger } from './shared/logger.js';
import { UserService } from './users/user-service.js';
import { MarketService } from './market/market-service.js';
import { TsetmcClient } from './market/tsetmc.client.js';
import { MarketMonitor } from './market/market-monitor.js';
import { MarketSchedule } from './market/market-schedule.js';
import { SnapshotRepository } from './database/repositories/snapshot.repository.js';
import { SnapshotCleanup } from './monitoring/snapshot-cleanup.js';
import { AlertService } from './alerts/alert-service.js';
import { AlertEngine } from './alerts/alert-engine.js';
import { NotificationDispatcher } from './notifications/notification-dispatcher.js';
import { ChannelService } from './alerts/channel-service.js';
import { channelVerifier } from './notifications/channel-permissions.js';

async function main() {
  const env = parseEnv(process.env);
  const logger = createLogger(env.LOG_LEVEL, env.BALE_BOT_TOKEN);
  const releaseLock = acquireInstanceLock(env.DATABASE_URL);
  let connection: ReturnType<typeof openDatabase>;
  try {
    connection = openDatabase(env.DATABASE_URL);
  } catch (error) {
    releaseLock();
    throw error;
  }
  const { db, sqlite } = connection;
  removeHeartbeat();
  const marketClient = new TsetmcClient({
    baseUrl: env.TSETMC_BASE_URL,
    timeoutMs: env.TSETMC_REQUEST_TIMEOUT_MS,
    maxRetries: env.TSETMC_MAX_RETRIES,
  });
  const authorizedUsers = [
    ...new Set([...env.BALE_ALLOWED_USER_IDS, ...env.BALE_ADMIN_USER_IDS]),
  ];
  const users = new UserService(db, authorizedUsers);
  // Deferred verification uses this same bot instance after initialization.
  const channels = new ChannelService(
    db,
    users,
    env.BALE_ADMIN_USER_IDS,
    env.BALE_CHANNEL_ID,
    (id, signal) => channelVerifier(bot.api, () => bot.botInfo.id)(id, signal),
    env.ALERT_DEFAULT_COOLDOWN_SECONDS,
  );
  const market = new MarketService(marketClient, env.MARKET_DATA_MAX_AGE_MS);
  const repository = new SnapshotRepository(
    db,
    env.SNAPSHOT_HISTORY_TOLERANCE_SECONDS * 1000,
  );
  const schedule = new MarketSchedule(
    env.MARKET_SESSIONS_JSON,
    env.MARKET_CLOSED_DATES,
    env.MARKET_MONITOR_FORCE_ACTIVE,
    env.MARKET_SESSION_MARKET,
  );
  const cleanup = new SnapshotCleanup(
    repository,
    env.SNAPSHOT_RETENTION_HOURS * 3_600_000,
    env.SNAPSHOT_CLEANUP_INTERVAL_MS,
    logger,
  );
  const alerts = new AlertService(
    db,
    users,
    env.ALERT_DEFAULT_COOLDOWN_SECONDS,
    env.ALERT_HISTORY_LIMIT,
  );
  const engine = new AlertEngine(
    db,
    repository,
    users,
    authorizedUsers,
    env.MARKET_DATA_MAX_AGE_MS,
    logger,
    Date.now,
    channels,
  );
  const monitor = new MarketMonitor(
    () => [...users.monitoredSymbols(), ...channels.monitoredSymbols()],
    market,
    repository,
    schedule,
    cleanup,
    {
      pollIntervalMs: env.MARKET_POLL_INTERVAL_MS,
      maxConcurrentRequests: env.MARKET_MAX_CONCURRENT_REQUESTS,
      requestTimeoutMs: env.MARKET_REQUEST_TIMEOUT_MS,
      maxAgeMs: env.MARKET_DATA_MAX_AGE_MS,
    },
    logger,
    Date.now,
    Math.random,
    (snapshot, sessionId) => engine.onSnapshot(snapshot, sessionId),
  );
  const bot = createBot(env, logger, {
    users,
    market,
    monitor,
    alerts,
    channels,
  });
  const dispatcher = new NotificationDispatcher(
    db,
    users,
    (chatId, text, signal) =>
      bot.api.sendMessage(
        chatId,
        text,
        {},
        signal as unknown as Parameters<typeof bot.api.sendMessage>[3],
      ),
    {
      maxRetries: env.NOTIFICATION_MAX_RETRIES,
      retryBaseMs: env.NOTIFICATION_RETRY_BASE_MS,
      maxAgeMs: env.NOTIFICATION_MAX_AGE_MINUTES * 60_000,
      marketMaxAgeMs: env.MARKET_DATA_MAX_AGE_MS,
    },
    logger,
    Date.now,
    channels,
  );
  const startupAbort = new AbortController();
  // grammY types use a legacy signal; the native signal has the required runtime API.
  const startupSignal = startupAbort.signal as unknown as Parameters<
    typeof bot.init
  >[0];
  let stopping = false;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  let shutdownDeadline: ReturnType<typeof setTimeout> | undefined;
  let stopPromise: Promise<void> | undefined;
  let monitorStop: Promise<void> | undefined;
  let dispatcherStop: Promise<void> | undefined;
  const startedAt = Date.now();
  let initialized = false;
  const bale = { lastSuccess: null as number | null, errors: 0 };
  bot.api.config.use(async (previous, method, payload, signal) => {
    try {
      const result = await previous(method, payload, signal);
      if (method === 'getUpdates' || method === 'getMe') {
        if (result.ok) {
          bale.lastSuccess = Date.now();
          bale.errors = 0;
        } else bale.errors++;
      }
      return result;
    } catch (error) {
      if (method === 'getUpdates' || method === 'getMe') bale.errors++;
      throw error;
    }
  });
  let databaseOk = true;
  const publishHealth = () => {
    try {
      const now = Date.now();
      db.insert(applicationState)
        .values({ key: 'database_health', value: 'ok', updatedAt: now })
        .onConflictDoUpdate({
          target: applicationState.key,
          set: { updatedAt: now },
        })
        .run();
      databaseOk = true;
    } catch (err) {
      if (databaseOk) logger.error({ err }, 'Database health probe failed');
      databaseOk = false;
    }
    writeHeartbeat(
      runtimeHealth(
        monitor.getStatus(),
        initialized,
        databaseOk,
        bale,
        startedAt,
        env.MARKET_REQUEST_TIMEOUT_MS,
      ),
    );
  };

  const shutdown = (signal: string) => {
    if (stopping) return;
    stopping = true;
    logger.info({ signal }, 'Stopping bot');
    shutdownDeadline = setTimeout(() => {
      logger.fatal('Graceful shutdown timed out');
      process.exit(1);
    }, 15_000);
    shutdownDeadline.unref();
    startupAbort.abort();
    monitorStop = monitor.stop();
    dispatcherStop = dispatcher.stop();
    marketClient.close();
    if (bot.isRunning()) {
      stopPromise = bot.stop().catch((err: unknown) => {
        logger.error({ err }, 'Failed to stop Bale polling');
        process.exitCode = 1;
      });
    }
  };
  const onSigint = () => shutdown('SIGINT');
  const onSigterm = () => shutdown('SIGTERM');
  process.once('SIGINT', onSigint);
  process.once('SIGTERM', onSigterm);

  try {
    monitor.start();
    publishHealth();
    heartbeat = setInterval(() => {
      try {
        publishHealth();
      } catch (err) {
        logger.error({ err }, 'Heartbeat failed');
        process.exitCode = 1;
        shutdown('heartbeat failure');
      }
    }, 15_000);
    await bot.init(startupSignal);
    if (stopping) return;
    // Bale does not document setMyCommands; commands remain available via /help.
    if (env.BALE_CHANNEL_ID && !stopping) {
      try {
        await channels.refresh(
          AbortSignal.any([startupAbort.signal, AbortSignal.timeout(10_000)]),
        );
      } catch {
        logger.warn(
          'Channel permissions unavailable; private alerts remain enabled',
        );
      }
    }
    if (stopping) return;
    await bot.start({
      allowed_updates: ['message', 'callback_query'],
      onStart: () => {
        if (stopping) return;
        initialized = true;
        dispatcher.start();
        publishHealth();
        logger.info(
          'Bot started; market monitor and notification worker enabled',
        );
      },
    });
  } catch (err) {
    if (!stopping) throw err;
    logger.debug({ err }, 'Bale request interrupted during shutdown');
  } finally {
    marketClient.close();
    clearInterval(heartbeat);
    await Promise.all([
      stopPromise,
      monitorStop ?? monitor.stop(),
      dispatcherStop ?? dispatcher.stop(),
    ]);
    clearTimeout(shutdownDeadline);
    process.off('SIGINT', onSigint);
    process.off('SIGTERM', onSigterm);
    try {
      removeHeartbeat();
    } finally {
      sqlite.close();
      releaseLock();
    }
    logger.info('Bot stopped; SQLite connection closed');
  }
}

await main().catch((err: unknown) => {
  createLogger().fatal({ err }, 'Application failed');
  process.exitCode = 1;
});
