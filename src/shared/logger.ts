import pino, { type DestinationStream } from 'pino';

export function createLogger(
  level = 'info',
  token = '',
  destination: DestinationStream = process.stdout,
) {
  const sanitize = (text: string) => {
    const redacted = token ? text.replaceAll(token, '[REDACTED]') : text;
    return redacted.replace(/\d+:[A-Za-z0-9_-]{30,}/g, '[REDACTED]');
  };

  return pino(
    {
      level,
      base: null,
      serializers: {
        err: (error: unknown) =>
          error instanceof Error
            ? {
                type: error.name,
                message: sanitize(error.message),
                stack: sanitize(error.stack ?? ''),
              }
            : { message: sanitize(String(error)) },
      },
    },
    destination,
  );
}
