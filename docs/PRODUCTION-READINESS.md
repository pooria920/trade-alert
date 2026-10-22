# Production verification

The former Node application has been replaced by Python at the repository root.
See [the current deployment guide](../DEPLOYMENT.fa.md),
[behavior coverage](../ACCEPTANCE.fa.md) and [migration history](../MIGRATION.fa.md).
Original applied SQL and journal files remain byte-identical under `migrations/`.
The tests compare Python against frozen independently captured TypeScript outputs;
Node and pnpm are no longer required. The live BestLimits limitation remains.
