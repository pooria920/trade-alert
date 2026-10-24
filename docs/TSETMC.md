# TSETMC observations

## Local in-session investigation, 2026-10-24

**Result: changing responses and agreement with the displayed order table do not
establish a fresh, complete, valid book.** Keep the live adapter's source
`timestamp: null`, `completeness: unknown`, and `verified: false`. No application,
detector, schedule, database, Telegram or deployment settings were changed.

### Capture and provenance

The local machine fetched four consecutive BestLimits responses for each symbol
on Saturday 2026-10-24, approximately **09:46:16–09:46:48 Asia/Tehran**
(06:16:16–06:16:48 UTC). There was a ten-second pause between rounds, with
sequential requests within each round. Each book was followed by a separate
closing-price request; dated static thresholds were fetched in rounds 1 and 4.
Search responses confirm the exact symbols and string instrument codes:
خودرو `65883838195688438`, فولاد `46348559193224090`.

All 22 recorded HTTP requests returned 200. Public response bodies are preserved
under [evidence/tsetmc-20261024/](evidence/tsetmc-20261024/), separately from tests
and synthetic detector inputs. The [manifest](evidence/tsetmc-20261024/manifest.json)
records URLs, client request/receipt times in UTC, selected response headers and
SHA-256 hashes of the saved UTF-8 response text. Capture used local PowerShell
`Invoke-WebRequest`, a 15-second timeout and normal TLS verification. The saved
bodies are response text, not packet captures or compressed wire bytes. No
credentials or private browser content were saved. An earlier exploratory فولاد
search succeeded but was not saved; it is not included in the 22 requests.

Contemporaneous closing responses report `dEven: 20261024` and `A `/`مجاز`.
Their `hEven` values change during the probe. The official pages also display
today's history date `1405/08/02` and changing morning prices/order volumes.
These observations support an in-session sample, rather than the earlier
closed-market sample; they do not verify an exchange calendar or a book source
time. No schedule bypass or application startup was used.

### Successive responses and same-day limits

All eight books contain ranks 1–5 with nonnegative price/volume/count fields.
Prices and every bid volume/count remain unchanged across all four rounds.
The following are the best ask's aggregate volume and order count, taken directly
from the saved responses; receipt times below omit fractional seconds in Tehran.

| Round | خودرو receipt | Ask volume / count at 659 | فولاد receipt | Ask volume / count at 4100 |
| --- | --- | --- | --- | --- |
| 1 | 09:46:16 | 2,034,008,232 / 1149 | 09:46:16 | 976,480,830 / 4185 |
| 2 | 09:46:27 | 2,033,381,368 / 1148 | 09:46:27 | 976,201,280 / 4189 |
| 3 | 09:46:37 | 2,032,988,008 / 1148 | 09:46:37 | 975,020,319 / 4185 |
| 4 | 09:46:47 | 2,030,025,062 / 1149 | 09:46:48 | 976,299,264 / 4196 |

Both threshold captures agree for each symbol. Select only the row dated
`20261024` (`hEven: 60125`); the same responses also include `20261021`.

| Symbol | Same-day lower–upper | Bid prices, ranks 1–5 | Ask prices, ranks 1–5 |
| --- | --- | --- | --- |
| خودرو | 659–699 | 658, 657, 656, 654, 650 | 659, 660, 661, 662, 663 |
| فولاد | 4100–4340 | 4000, 3990, 3880, 3660, 3580 | 4100, 4110, 4120, 4130, 4140 |

**Every returned bid in both symbols is below the same-day lower boundary.**
Asks lie within the selected limits. This repeats the previous inconsistency
during observed trading activity. It is not resolved by changing receipt times,
using yesterday's limits, filtering away bids, or substituting closing-price
`priceMin`/`priceMax`. Threshold rows and closing responses contain `insCode: "0"`,
while book rows contain `insCode: null`; instrument binding is provided by the
request URL/search selection, not a self-identifying book payload.

### Comparison with the visible order tables

The official [خودرو page](https://www.tsetmc.com/instInfo/65883838195688438)
and [فولاد page](https://www.tsetmc.com/instInfo/46348559193224090) were read in
local Chrome. The [saved DOM table observations](evidence/tsetmc-20261024/browser-tables.json)
record five rendered rows per symbol, column labels, displayed limits/state,
observation UTC time and the separately labelled latest-price-information time.
The browser tables were sampled later than the API rounds, not atomically.

At 09:47:32.896 Tehran, خودرو shows the same five bid/ask prices and all bid
volumes/counts as the API captures; ranks 2–5 also retain the same ask
volumes/counts. Its best ask is now 2,040,962,538 / 1156. At 09:47:40.056,
فولاد likewise retains all bid prices/volumes/counts and ask prices; its best ask
is 977,433,167 / 4216. Its asks at 4120, 4130 and 4140 have also changed in
volume/count since round 4. Displayed limits match the dated API limits exactly.
Thus the public table corroborates the field orientation and the below-limit
bids, but does not cure the inconsistency. The page may share the API's upstream
data; this is not an independent exchange feed or proof of complete-book semantics.
Different ask volumes across these nonsimultaneous observations are not evidence
of API/UI disagreement.

### Source time, completeness and deletion semantics

- No captured BestLimits body has a date, source time, sequence/update ID,
  reset flag, completeness flag or deletion marker. Every row's `title` and
  `insCode` are null. Changing aggregates demonstrate changing responses only;
  freshness, maximum latency and atomicity of the two sides remain unmeasurable.
- The HTTP `Date` header is approximately eight seconds behind the client receipt
  clock for these samples. No `Age`, `Cache-Control`, `ETag`, `Last-Modified`,
  `Via` or `X-Cache` header was observed. Neither the difference nor absence of
  cache headers proves cache age, exchange latency or synchronized clocks.
- Closing `hEven` for خودرو is 09:45:52, 09:46:10, **09:45:52**, 09:46:28:
  a later response regresses. فولاد reports 09:45:58, 09:46:06, 09:46:16,
  09:46:29. The UI's separately labelled price-information times are 09:47:08
  and 09:47:13 at the saved observations. None can timestamp BestLimits.
- All responses repeat five populated rows, including unchanged ranks. This is
  compatible with repeated top-five snapshots, but does not prove a complete
  book, replacement semantics, or how fewer levels are represented. No rank
  disappears, shifts price, becomes zero, or resets in these captures.
- فولاد's rank-2 ask at 4110 falls from 5,504,196 / 28 to 5,304,196 / 27
  between rounds 3 and 4. خودرو's best ask count also falls from 1149 to 1148.
  These are aggregate reductions, not identified order cancellations: executions,
  cancellations, amendments and combinations cannot be distinguished. There are
  no individual order IDs or operation types. A full price-level deletion,
  empty-side reset, and whether omitted levels mean unchanged or deleted remain
  **unobserved and unresolved**. Do not merge partial books or infer deletion.

This bounded observation cannot settle deletion/reset semantics in the absence of
such a transition. A future local capture must observe a level leaving/re-entering
the book or an empty-side/reset, alongside authoritative source timing and
instrument/date binding. Even then, shared UI/API behavior alone is not a semantic
contract. Until the outstanding requirements are verified, these books must not
produce queues, definitive no-queue states, stored accepted snapshots or alerts.

### Local verification of this documentation change

All 22 saved bodies parsed as JSON and matched their manifest SHA-256 hashes;
the saved browser-table JSON also parsed successfully. `git diff --check` passed.
No test expectations were generated from the application and no live requests
were made from tests.

The checkout initially had no `.venv`. A local Python 3.14.7 environment was
created, but installing `requirements-dev.txt` failed because the package index
could not supply `anyio==4.15.1`. A separate attempt to install the pinned check
tools failed at `mypy==2.4.0` (no matching distribution). All required commands
were nevertheless attempted with `.venv/Scripts/python.exe`: mypy and both ruff
commands failed because those modules were absent; unittest discovery reported
36 tests with 18 errors from missing dependencies (including Telethon).
`pip check` passed in the incomplete environment and therefore does not establish
that project dependencies are installed. `compileall -q trade_alert tests`
passed. Full project validation remains blocked on dependency installation;
dependency pins and application code were left unchanged.

## Earlier observations, 2026-10-21

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
