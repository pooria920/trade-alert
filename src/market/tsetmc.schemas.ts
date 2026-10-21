import { z } from 'zod';
import { insCodeSchema } from './market.types.js';

const quantity = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
export const searchResponseSchema = z.object({
  instrumentSearch: z
    .array(
      z.object({
        insCode: insCodeSchema,
        lVal18AFC: z.string().trim().min(1).max(80),
        lVal30: z.string().trim().min(1).max(200),
      }),
    )
    .max(2000),
});

export const bestLimitsResponseSchema = z.object({
  bestLimits: z
    .array(
      z.object({
        number: z.number().int().positive().max(100),
        pMeDem: z.number().nonnegative(),
        qTitMeDem: quantity,
        zOrdMeDem: quantity,
        pMeOf: z.number().nonnegative(),
        qTitMeOf: quantity,
        zOrdMeOf: quantity,
      }),
    )
    .max(100),
});

export const limitsResponseSchema = z.object({
  staticThreshold: z
    .array(
      z.object({
        dEven: z.number().int().min(19000101).max(99991231),
        hEven: z.number().int().nonnegative().max(235959),
        psGelStaMax: z.number().positive(),
        psGelStaMin: z.number().positive(),
      }),
    )
    .max(1000),
});

export const closingResponseSchema = z.object({
  closingPriceInfo: z.object({
    dEven: z.number().int().min(19000101).max(99991231),
    hEven: z.number().int().nonnegative().max(235959),
    instrumentState: z.object({ cEtaval: z.string().max(20) }).nullable(),
  }),
});
