import type { Context, MiddlewareFn } from 'grammy';

export function authorize(
  allowedUserIds: readonly string[],
): MiddlewareFn<Context> {
  const allowed = new Set(allowedUserIds);
  return async (ctx, next) => {
    if (
      !ctx.from ||
      !allowed.has(String(ctx.from.id)) ||
      ctx.chat?.type !== 'private' ||
      ctx.chat.id !== ctx.from.id
    )
      return;
    await next();
  };
}
