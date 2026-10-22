import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const runtime = vi.hoisted(() => ({
  init: vi.fn<(signal?: AbortSignal) => Promise<void>>(),
  start: vi.fn<(options: { onStart: () => void }) => Promise<void>>(),
  stop: vi.fn<() => Promise<void>>(),
  isRunning: vi.fn<() => boolean>(),
  setMyCommands: vi.fn<() => Promise<true>>(),
  close: vi.fn(),
  writeHeartbeat: vi.fn(),
  removeHeartbeat: vi.fn(),
}));

vi.mock('../src/bot/bot.js', () => ({
  createBot: () => ({
    ...runtime,
    api: { setMyCommands: runtime.setMyCommands, config: { use: vi.fn() } },
  }),
}));
vi.mock('../src/database/client.js', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../src/database/client.js')>();
  return {
    openDatabase: () => {
      const connection = actual.openDatabase(':memory:');
      return {
        db: connection.db,
        sqlite: {
          close: () => {
            connection.sqlite.close();
            runtime.close();
          },
        },
      };
    },
  };
});
vi.mock('../src/shared/health.js', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../src/shared/health.js')>()),
  writeHeartbeat: runtime.writeHeartbeat,
  removeHeartbeat: runtime.removeHeartbeat,
}));
vi.mock('../src/database/instance-lock.js', () => ({
  acquireInstanceLock: () => () => {},
}));

let sigterm = () => {};

beforeEach(() => {
  vi.resetModules();
  vi.resetAllMocks();
  vi.stubEnv('BALE_BOT_TOKEN', `123456:${'x'.repeat(35)}`);
  vi.stubEnv('BALE_ALLOWED_USER_IDS', '123456789,987654321');
  vi.stubEnv('BALE_ALLOWED_USER_ID', undefined);
  vi.stubEnv('BALE_ADMIN_USER_IDS', '');
  vi.stubEnv('BALE_CHANNEL_ID', '');
  vi.stubEnv('LOG_LEVEL', 'silent');
  vi.stubEnv('MARKET_SESSIONS_JSON', '[]');
  vi.stubEnv('MARKET_MONITOR_FORCE_ACTIVE', 'false');
  const originalOnce = process.once.bind(process);
  vi.spyOn(process, 'once').mockImplementation((event, listener) => {
    if (event === 'SIGTERM') {
      sigterm = () => listener();
      return process;
    }
    if (event === 'SIGINT') return process;
    return originalOnce(event, listener);
  });
  runtime.init.mockResolvedValue(undefined);
  runtime.setMyCommands.mockResolvedValue(true);
  runtime.isRunning.mockReturnValue(true);
});

afterEach(() => {
  vi.restoreAllMocks();
});

it('awaits Bale stop completion before closing SQLite on SIGTERM', async () => {
  let stopCompleted = false;
  let closedAfterStop = false;
  runtime.stop.mockImplementation(
    () =>
      new Promise((resolve) => {
        setImmediate(() => {
          stopCompleted = true;
          resolve();
        });
      }),
  );
  runtime.close.mockImplementation(() => {
    closedAfterStop = stopCompleted;
  });
  runtime.start.mockImplementation((options) => {
    options.onStart();
    sigterm();
    return Promise.resolve();
  });
  await import('../src/main.js');
  expect(closedAfterStop).toBe(true);
  expect(runtime.stop).toHaveBeenCalledOnce();
  expect(runtime.close).toHaveBeenCalledOnce();
  expect(runtime.writeHeartbeat).toHaveBeenCalledTimes(2);
  expect(runtime.removeHeartbeat).toHaveBeenCalledTimes(2);
  expect(runtime.setMyCommands).not.toHaveBeenCalled();
});

it('cancels initialization and closes SQLite if SIGTERM arrives before polling', async () => {
  runtime.isRunning.mockReturnValue(false);
  runtime.init.mockImplementation(
    (signal) =>
      new Promise((_resolve, reject) => {
        signal?.addEventListener(
          'abort',
          () => reject(new Error('Initialization aborted')),
          { once: true },
        );
        queueMicrotask(sigterm);
      }),
  );
  await import('../src/main.js');
  expect(runtime.start).not.toHaveBeenCalled();
  expect(runtime.stop).not.toHaveBeenCalled();
  expect(runtime.close).toHaveBeenCalledOnce();
});
