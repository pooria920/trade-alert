# TSETMC observations, 2026-10-21

Phase 4 adds rule evaluation and Telegram outbox delivery but does not change the evidence below. The live adapter still cannot produce verified queue snapshots, so it cannot trigger alerts. Synthetic normalized test inputs verify the algorithm and delivery wiring only.

Phase 5 reuses that collector for channel/composite alerts and preserves the same safeguard. Minimum current queue volume is derived only from validated queue snapshots; it is not traded volume. Neither private nor channel live alerts can fire until book timestamps/completeness are verified. No new live TSETMC probe or Telegram channel post was performed for this phase.

These are live observations from this development machine, not an upstream API contract. Raw HTTP 200 payloads are preserved in `tests/fixtures/tsetmc/`. The browser research tool could not access these endpoints; direct HTTPS requests from the host succeeded on retry. An initial search request timed out after 12 seconds.

The compiled native-Fetch client was also exercised against live endpoints on this date: search returned 41 instruments; the selected خودرو code returned five levels and the dated limits below. The detector returned `unverified-book`, as intended. This verifies transport/parsing for those requests, not actionable queue semantics or service availability for all instruments.

| Request under `https://cdn.tsetmc.com/api/`                | Observed result                                                                                       |
| ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `Instrument/GetInstrumentSearch/خودرو` (URL encoded)       | 200, `instrumentSearch` array; `insCode` is a string, `lVal18AFC` is the symbol, `lVal30` is the name |
| `BestLimits/65883838195688438`                             | 200, `bestLimits` array containing levels numbered 1–5                                                |
| `MarketData/GetStaticThreshold/65883838195688438`          | 404; not a usable endpoint in this test                                                               |
| `MarketData/GetStaticThreshold/65883838195688438/20261021` | 200, `staticThreshold` array with rows for both 20261020 and 20261021                                 |
| `MarketData/GetStaticThreshold/65883838195688438/0`        | 200, empty `staticThreshold` array; zero must not be assumed to mean today                            |
| `ClosingPrice/GetClosingPriceInfo/65883838195688438`       | 200, `closingPriceInfo`, `dEven: 20261021`, `hEven: 123002`, state code `A ` and title `مجاز`         |

Order fields observed: `pMeDem`, `qTitMeDem`, `zOrdMeDem` for bid price, volume and count; `pMeOf`, `qTitMeOf`, `zOrdMeOf` for ask. `number` is the level rank. These label interpretations are provisional; the response provides no semantic contract. The raw order-book rows have `insCode: null`, no date, and no time.

Daily boundary fields are `psGelStaMax` and `psGelStaMin`. Select a row matching the requested Tehran Gregorian date (`dEven`) and the latest `hEven`, never the first row or a previous date. The 20261021 row gives upper 719 and lower 679. Closing-price `priceMin`/`priceMax` are not substituted for these boundaries.

Two consecutive BestLimits requests returned identical five-level books. This does **not** establish complete-snapshot semantics: the market was closed, and no incremental transition was observed. The current adapter sets `completeness: unknown`, `verified: false`, and the book source `timestamp: null`. The HTTP receipt time is recorded separately. Closing-price times cannot date an independently fetched order book; `A` is treated as a provisional open-state mapping, not evidence that a session is currently open.

There is an actual inconsistency in these captures: the best bid is 675, below today's lower limit 679, and several other bids are also below it. This could reflect different update times or another undocumented behavior. Queue detection must refuse this data, including a definitive “no queue.” `/queue` therefore reports that the book cannot be verified instead of showing invented queue volumes.

To enable live classification, independently verify source book timestamps, complete/incremental semantics (including clears/deletions), instrument/date binding, state-code semantics, and coherence with same-session limits. Then update the adapter and add captured transition fixtures. Do not add an environment switch that bypasses verification. Unit-test normalized books marked verified are synthetic detector cases, not claims about live TSETMC data. The fixture-backed provider is explicitly unverified and cannot produce actionable queue classifications.

The production client uses the observed routes and validated fields, native Fetch, bounded retries (timeouts/network failures, 429 and transient 5xx only), exponential backoff, and Retry-After where within its cap. No upstream polling rate has been verified. Phase 2 performs on-demand requests only.

Phase 3 adds scheduled collection but does not change those verification findings. Unknown/incremental books, null source timestamps and inconsistent limits remain non-actionable, so the current live adapter produces no stored snapshots. Retry backoff now includes jitter; known server cooldown applies across calls, and monitor deadlines cancel HTTP/retry waits without losing Retry-After information. Session days/hours/closure dates are explicitly operator-configured, not a verified exchange calendar. The default session list is empty and force-active is false.
