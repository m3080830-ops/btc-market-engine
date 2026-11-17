# Multi-account migration plan (zero-interruption)

## Non-negotiable production safeguards

- The currently deployed `main` revision remains untouched and continues serving live trading.
- All multi-account work is isolated on `multi-account-foundation`; no deployment from this branch.
- Never change `strategy.py`, signal selection, entry timing, structural SL, trailing calculations, R calculations, or existing risk parameters as part of this migration.
- Do not point production traffic at the new implementation until the full regression, isolation, recovery, and load-test gates pass and the owner explicitly approves cutover.
- Do not migrate, rewrite, or delete production trading data during development.
- Keep the existing single-account path operational as the fallback until a separately approved migration is complete.

## Current baseline (main at 9a675bcf76d823c6671d6e5c3bde62289628b0c2)

- `strategy.py` is the strategy boundary and exposes the existing setup/entry/SL planning functions.
- `main.py` owns the live refresh loop, symbol-scoped global executor registry, SQLite setup/history state, position management, dashboard, and HTTP handler.
- `mexc_futures_live.py` owns MEXC Futures signing, sizing, order placement, position reconciliation, and exchange SL confirmation. Its constructor currently reads one global `MEXC_ACCESS_KEY` / `MEXC_SECRET_KEY` pair.
- The current executor registry is keyed by symbol, not by account plus symbol.
- Existing SQLite tables and dashboard state are not tenant-scoped.
- Existing tests cover strategy variants, sizing, MEXC exchange authority, SL confirmation, trailing-related behavior, symbol safeguards, and stale-entry prevention. They do not yet prove tenant isolation or 100-account fanout.

## Target separation

1. Shared market-data ingestion and one central strategy evaluation per symbol.
2. Immutable/versioned strategy signal records, retaining the current strategy output and timestamps.
3. A separate execution task for each enabled account and signal.
4. An isolated executor context per `(account_id, symbol)`, with that account's credentials, balance, positions, order IDs, SL state, idempotency keys, and execution ledger.
5. Tenant-scoped persistence and API authorization; clients can only read their own account data.
6. Admin-only controls for account status and centrally managed strategy/risk policy.

## Delivery batches

### Batch 0 — Baseline and safe workspace
- Preserve the production `main` branch and deployed revision.
- Create this isolated feature branch from the exact baseline commit.
- Record current regression tests and capture strategy-output fixtures before adapter refactoring.
- No production data changes, no live orders, and no deployment.

### Batch 1 — Characterization tests
- Add deterministic tests that lock current strategy outputs for representative BTC/ETH candle fixtures.
- Add tests that lock current entry freshness, position sizing, initial SL, SL confirmation, and trailing decisions.
- These tests must pass unchanged before and after later refactors.
- Test-only changes; no runtime behavior changes.

### Batch 2 — Account-aware execution boundary
- [x] Add a standalone `account_execution.py` module with validated `ExecutionScope(account_id, symbol)`, immutable runtime-only `AccountExecutionContext`, and a thread-safe exact-scope `AccountExecutorRegistry`.
- [x] Add 8 offline tests for account/symbol separation, unsupported symbols, missing credentials, safe credential repr, no cross-account fallback, duplicate registration, and exact-scope removal.
- [ ] Next: add a compatibility wrapper around the existing MEXC adapter, with explicit credential injection and no changes to sizing, order payloads, entry timing, SL, or trailing calculations.
- [ ] Add fake-exchange tests proving account-specific order/SL operations cannot cross account boundaries.
- This first slice is not wired into `main.py`; no production wiring or live customer credentials.

### Batch 3 — Persistence isolation
- Introduce tenant-aware repository interfaces and a migration plan to PostgreSQL.
- Separate shared market signals from account-specific execution, position, stop, balance snapshots, and audit records.
- Use additive schema migrations and dual-read/dual-write only in test/staging until verified.
- Never alter or delete the existing production SQLite data as part of development.

### Batch 4 — Fanout and fault isolation
- Add a bounded worker/queue model to dispatch each signal independently to enabled accounts.
- Add idempotency, stale-signal expiry, per-account rate limits, timeouts, retries only for safe reads, and account-level circuit breakers.
- One account failure must not block another account or the central market/strategy loop.
- Load-test 100 simulated accounts before any exchange-backed pilot.

### Batch 5 — Authentication, secrets, and customer/admin panels
- Add authentication and server-side tenant authorization to every private endpoint.
- Store per-account API credentials in a managed secret store or envelope-encrypted vault; never return secrets to clients or logs.
- Require least-privilege MEXC keys, no withdrawal/transfer permissions, and verify IP restrictions where supported.
- Add customer self-service and owner-only administration.

### Batch 6 — Staging, pilot, and controlled cutover
- Deploy only to a separate staging service with separate database and credentials.
- Run 100-account simulation, failure/restart recovery, isolation, security, and regression gates.
- Pilot with one separately approved test account; compare signal and SL decisions against the unchanged baseline.
- Cut over only with explicit owner approval, a tested rollback path, and no unresolved position/SL reconciliation errors.

## Required release gates

- Strategy fixtures produce byte-for-byte equivalent decision fields for the same candle inputs.
- Existing regression suite passes.
- No cross-tenant read, write, credential, order, position, or stop access in adversarial tests.
- Duplicate/replayed signals cannot create duplicate account orders.
- MEXC position and active SL are confirmed against the correct account before a trade is shown as protected/open.
- A failed or slow account cannot block other accounts.
- 100 simulated accounts pass load and recovery tests.
- Production rollback restores the existing single-account service without database rollback or loss of exchange state.

## Current status

- Batch 0 workspace created on `multi-account-foundation`.
- Batch 1 added `tests/test_strategy_contract.py` with 8 compatibility checks for next-candle entry selection, existing target/RR calculations, structural SL calculation, trend-before-range selection, and Variant C veto behavior. GitHub Actions run #139 completed successfully.
- Batch 2 added the isolated account/symbol context and executor registry in `account_execution.py`, plus 8 tests in `tests/test_account_execution.py`. These primitives are standalone and not connected to live execution.
- A new GitHub Actions run has been triggered for the account-scope tests; its result will be checked before further refactoring.
- No production runtime path, strategy, adapter, database, Cloud Run service, credentials, or live positions changed.
- No production branch, Cloud Run service, credentials, database, or live positions changed.
