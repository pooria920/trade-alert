# Frozen TypeScript regression reference

Captured offline on 2026-10-22 from the Node implementation at commit `a8001f6`,
before its removal. The capture used the existing migration harnesses in
`python/tests/*reference.mjs` (working-tree files, not part of that commit).
They invoked original modules without starting the app; the delivery harness
included its missing `node:process` import. No live API or production DB was used.

The full 163-test Python suite passed during capture. Files pair exact synthetic
inputs with independent TypeScript outputs. `database.json` records the original
schema and migration hashes from a temporary Drizzle database. Captured upstream
payloads remain separate under `../tsetmc` and are unverified. Credentials and IDs
in configuration scenarios are synthetic.

`tests/reference.py` requires an exact input match and fails on unknown cases.
Tests still construct inputs and compare Python results against these outputs.
Never regenerate these files from the application under test. New behavior needs
explicit tests and reviewed expectations. This is frozen regression coverage,
not ongoing cross-language execution.
