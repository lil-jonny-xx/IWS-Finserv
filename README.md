# MIS Portal

Internal portfolio management and analytics system for a private family office and its linked investment entities. Automates the full data pipeline — CAMS CAS statement ingestion, domestic + international broker sync, PMS scraping, sub-second live trade capture, NAV/FX/market-data enrichment, dividend derivation and benchmark tracking — and presents consolidated holdings, P&L, realised gains and analytics across every asset class through a Next.js dashboard with a built-in AI analyst.

The portal tracks several individual and corporate entities, grouped under a small number of tax-filing groups. **The entity roster, the group structure, tax identifiers, account numbers and all mailbox addresses live in the database** (`pan_group`, `entity`) — never in this repository, in `.env`, or in any committed file. Throughout this document a per-entity identifier is written as the placeholder `{E}`.

---

## What it does

### Tracked asset classes

- **Mutual funds** — automated nightly ingestion from CAMS CAS PDFs via a central Gmail inbox; parsed per entity by folio holder-name matching; CAGR/XIRR, realised & unrealised P&L, exposure, FY-by-FY returns. Inception date resets on a full redemption so a re-entered fund is measured from its current lot, not its 2013 first buy.
- **Equity (domestic)** — holdings sync from Angel One (SmartAPI), Zerodha (Kite Connect) and Dhan; live LTP every minute during market hours; FIFO transaction ledger driving inception XIRR/CAGR; per-(entity, broker) free cash; today's unsettled (T1) positions shown beside the settled holding.
- **Foreign equity** — international brokers (Interactive Brokers Flex, Vested, DBS) in native currency, FX-converted to INR; prices refreshed from an independent feed (Finnhub → Twelve Data → yfinance) decoupled from the broker sync; its own page so the Equity page stays India-only.
- **Futures & Options** — `fno_position` / `fno_account`, fed by manual entry today and by the Share India uTrade scraper (`shareindia_fno_worker.py`, recon stage) once the portal schema is mapped.
- **PMS** — Nuvama WealthSpectrum and ICICI Prudential PMS, both Playwright-scraped (neither exposes an API) into `pms_holding` / `pms_realised`, segmented per provider with overall / per-entity / per-PMS P&L.
- **Commodities** — gold and silver holdings (physical, ETF and fund) on their own page with the full equity metric set; the market rail carries ₹ spot (`gold-api`, Indian trade units) alongside COMEX futures.
- **Unlisted / startup** — `unlisted_round` / `unlisted_event`: priced round-by-round with a per-round cap table rather than a single manual value.
- **Property register** — land and building assets with per-floor economics, tenure, nature multi-select, document checklist, image gallery, purchase/sale cost columns and a derived capital gain. Fair value = area × Ready Reckoner rate × 1.75 (a manual government-rate basis; no scrapers or valuation APIs). Two valuation amounts + reports are stored per property. **Excluded from overview totals by design.**
- **Art & Collectibles** — separate registers with photo galleries and lightbox viewing; excluded from portfolio totals.
- **Bank accounts** — cash-only accounts fed by uploaded statements (PDF/CSV/Excel, parse-then-confirm) or manual entry; fold into the dashboard CASH bucket.
- **Other assets** — manual inputs (PPF, liquid/debt/arbitrage funds, AIF, overseas funds, forex, funds-in-transit, broker balances) folded into the same asset-class buckets. Categories without a dedicated page auto-surface as a nav tab at `/assets/<category>` once the first entry exists.

### Analytics & income

- **Analytics** — today's P&L, inception P&L, YTD P&L, CAGR/XIRR, asset-class allocation, weekly delta, FX-adjusted values; per-entity money-weighted XIRR from real external cash flows (`portfolio_returns`). Annualised CAGR/XIRR appear only once a holding is held ≥ 1 year — below that the absolute return is shown instead.
- **Realised gains** — Indian equity realised P&L on **FIFO** with a short-term / long-term split (foreign stays average-cost). Four views: by entity, by demat (per-(entity, broker) FIFO via `?group=broker`), year-on-year (FY × entity), and Dividends. Commodities get their own section.
- **Dividends** — Indian dividends never reach the broker (the company credits the shareholder's bank directly), so they are *derived*: Yahoo ex-date + rate/share × quantity held on that date, replayed over the same `stock_transaction` ledger FIFO uses, across every security ever traded. A gross estimate, not cash received — TDS is not modelled.
- **Corporate actions** — splits and bonuses are depository-credited, so no broker feed reports them and each one mints a **new ISIN**, splitting the lot pool. `corporate_actions.py` records verified actions, detects held-but-unrecorded splits, and FY-returns/history reconstruction are split-aware.
- **Broker P&L statement reconciliation** — Zerodha / Angel One / Dhan realised-P&L statements imported as a per-scrip *oracle* (`broker_pnl_statement` / `broker_pnl_line`), diffed against our FIFO and classified: MATCH, ISIN_MIGRATION, CA_COST_DRIFT, SELL_GAP, NO_DATA. Cost-drift fixes are backfilled behind a yfinance ex-date/ratio gate; sell gaps are never auto-fixed.
- **Benchmarks & market data** — a live market rail spanning Indian and world indices, FX pairs, crypto, US treasuries, commodities, US mortgage/CD rates (FRED) and India/US CPI + India WPI (from the Office of the Economic Adviser direct, not the IMF, whose India WPI series stalled). Per-security proxy benchmarks.
- **Reports** — on-demand XLSX (per entity / consolidated) with a broker rollup, PMS, broker & bank cash and real-estate lines; standalone register workbooks for properties and collectibles.
- **AI analyst (Jarvis)** — streaming agentic assistant over the Claude Messages API; answers natural-language questions using DB tools (holdings, cash, PMS, transactions, performance, sector exposure, fundamentals), quantitative tools (concentration, correlation, volatility, drawdown, scenario), chart rendering, plus Anthropic-hosted web search and code execution. Always all-entities; property and collectibles are deliberately out of scope.
- **Operations** — a staleness monitor with seven independent checks emails on genuine breakage, and a nightly cyclical backup snapshots the database and the uploads tree.

### Platform

- **Multi-entity** — a global multi-select entity switcher on every asset page; the backend scopes each page endpoint with `entity_id: List[int]` + `= ANY()`. Empty selection means all entities.
- **Dynamic navigation** — one global sticky nav; `/api/v1/nav-coverage` hides sections with no data and hides entity pills where that entity has nothing.
- **Roles** — admin sees everything; a member login sees its own entity across every section. Only Manual Data and Trades are admin-only (enforced in the API, not just the nav).
- **Privacy glass** — a one-click blur over headline totals and the asset-class breakdown, for screen-sharing.
- **Security** — JWT auth with token versioning, Redis blacklist, bcrypt passwords, self-service password change + admin-mediated reset, per-request CSP nonce, rate limiting, upload whitelisting, account lockout, audit log.

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI (Python 3.12), Gunicorn + Uvicorn workers |
| Frontend | Next.js 16.2, React 19.2, TypeScript 5, Tailwind CSS 4 |
| Database | PostgreSQL 16 (66 tables) |
| Realtime | Broker order-update WebSockets → Redis pub/sub → SSE |
| AI | Claude Messages API (`anthropic` SDK) — streaming tool-use loop |
| Market data | AMFI / mfapi.in (MF NAV), Finnhub / Twelve Data / yfinance (foreign equity), broker LTP (domestic), Yahoo (indices/FX/commodities), gold-api (₹ spot metals), FRED, IMF, eaindustry.nic.in |
| Auth | JWT (2h TTL) + Redis blacklist + httpOnly cookies |
| Automation | Playwright (Chromium), playwright-stealth, Xvfb (CAMS, Nuvama, ICICI PMS, Vested, DBS, Share India) |
| Email | Gmail API (OAuth 2.0) |
| Reverse proxy | Nginx + Cloudflare |

---

## Install

Python deps are fully pinned at the repo root for reproducible deploys:

```bash
/var/www/.venv/bin/pip install -r requirements.txt      # workers (cron/systemd venv)
/var/www/mis-portal/venv/bin/pip install -r requirements.txt   # API venv (gunicorn runs from here)
cd iws-portal-frontend && npm ci && npm run build
```

> **Two virtualenvs.** The API service runs from `mis-portal/venv`; cron jobs and systemd timers run from `/var/www/.venv`. Installing a new dependency in only one of them breaks the other half of the system.

Regenerate the pin after a dependency change with `/var/www/.venv/bin/pip freeze > requirements.txt`. Keep `dhanhq` pinned — an unpinned upgrade previously broke the equity price worker's Dhan adapter silently.

### Deploying the frontend

Restarting `iws-frontend.service` alone serves the **old** `.next` build. Always build first, as the service user:

```bash
sudo -u www-data npm run build      # in /var/www/iws-portal-frontend
sudo systemctl restart iws-frontend
```

Run schema migrations **before** restarting the backend, not after — a restart-before-migrate has 500'd live pages.

---

## Architecture

### Request flow

```
Internet
  │
  ▼
Cloudflare  (TLS termination, DDoS protection)
  │
  ▼
Nginx :443  (SSL via Cloudflare origin cert; HSTS, Referrer-Policy;
             client_max_body_size 26m on /api/ — the default 1 MB silently
             turned large uploads into an HTML 413)
  │
  ├── /        ──► Next.js :3000  (iws-frontend.service)
  │                middleware.ts injects a per-request CSP nonce (CSP lives in
  │                proxy.ts, not the nginx config)
  │                Pages: / (login)  /dashboard  /equity  /foreign-equity
  │                       /mutual-funds  /fno  /bank-accounts  /pms
  │                       /gold-silver  /unlisted  /properties  /art
  │                       /collectibles  /realised-gains  /trades
  │                       /manual-data  /reports  /assistant  /account
  │                       /forgot-password  /assets/[category]  (dynamic)
  │
  └── /api/    ──► Gunicorn + Uvicorn :8000  (mis-portal.service, 3 workers)
                   FastAPI app — mis-portal/main.py (89 routes under /api/v1)
                     auth (login/logout/change-password/forgot-password/
                       admin-reset/users/reset-requests), me, entities,
                       nav-coverage, overview, holdings (MF), holdings/combined,
                       equity (+summary, +activity), foreign-equity (+dbs
                       preview/commit), fno, gold-silver, pms, unlisted-rounds,
                       bank-accounts (+statements), properties (+documents,
                       images, floors, entities, nature-types, sell/unsell),
                       art-detail, manual-inputs, manual-assets,
                       manual-attachments, manual-trades, trades/tradebook
                       (preview/commit), transactions, realised-gains
                       (+pnl-statement preview/commit), dividends,
                       corporate-actions, live/trades (SSE), benchmarks,
                       fx-rates, reports, assistant, dhan/zerodha postbacks,
                       health
```

### Uploads

One canonical tree at `/var/www/uploads`, organised by schema: `properties/<id>/<doc_type>/`, `manual/<entity>/<category>/`, `bank-statements/`. **Files are not stored in Postgres** — only their metadata. With `UPLOADS_XACCEL=1` plus an internal nginx location, files are served via X-Accel-Redirect. Per-file cap 200 MB.

### Trade capture — three tiers

Five capture paths feed `stock_transaction`, organised so a later, more authoritative tier supersedes an earlier one. `source_ref` is scoped `{broker}:{entity}:{date}:{trade_id}` — broker trade IDs repeat per day, and the old unscoped key silently dropped 76 real trades.

| Tier | Path | Latency | Authority |
|---|---|---|---|
| 1 | `live_trade_daemon.py` — broker order-update WebSocket → DB + Redis → SSE | sub-second | provisional |
| 2 | `equity_snapshot_worker.py` — position snapshots diffed into buys/sells (`source='snapshot'`, opening seed `source='snapshot_open'`) | hourly | provisional |
| 3 | `broker_txn_sync_worker.py` / tradebook + ledger import / manual `/trades` register | post-close | **authoritative** |

This makes the tradebook self-building: metrics work from snapshot seed/diff with no imports at all, and broker P&L statements act as a fourth, oracle-grade cross-check.

### Data pipelines

Cron workers are launched by `workers/cron_wrapper.py` (shared venv + logging entrypoint); higher-frequency workers run as systemd timers. The server runs in **UTC**; IST equivalents are shown in the schedule below.

```
NIGHTLY CAS PIPELINE
  cas_automation_worker.py  (orchestrator)
    ├── cams_trigger_worker.py   Playwright fills the CAMS CAS form per entity
    ├── gmail_worker.py          Gmail API polls the collector inbox for the PDF
    └── cas_parser_worker.py     PyMuPDF + casparser → upsert holdings/txns
                                 (folio + description are re-rendered
                                 inconsistently by CAS — both are normalised
                                 before use as identity keys)

EQUITY PIPELINE (domestic)
  token_refresh_worker.py     refreshes broker OAuth/TOTP tokens (01:00 UTC)
  equity_sync_worker.py       holdings + broker cash from Angel One, Zerodha, Dhan
                              (pre-market + a 10:00 IST catch-up run)
  equity_price_worker.py      live LTP every minute (systemd timer; market-hours
                              guarded); also refreshes quantity/avg_cost and picks
                              up new positions (refresh_holdings_light)
  equity_snapshot_worker.py   intraday position snapshots + trade detection
  live_trade_daemon.py        one process per (entity, broker); order-update WS →
                              stock_transaction + Redis 'live_trades' channel
  broker_txn_sync_worker.py / stock_transaction_worker.py   authoritative txns
  import_tradebooks_multi.py + import_ledgers.py + symbol_bridge.py   bulk import
  manual_positions_worker.py  materialises the manual /trades register into
                              equity_holding so it prices live
  equity_txn_metrics_worker.py   FIFO inception XIRR/CAGR/YTD (nightly + 12:00
                                 and 16:00 IST intraday refreshes)
  snapshot_xirr_worker.py     snapshot-derived XIRR fallback where no tradebook exists
  fy_returns_worker.py        financial-year growth columns (split-aware)
  portfolio_returns_worker.py per-entity money-weighted XIRR from external_cashflow
  reconstruct_history.py      self-made history for NRI transfer-in holdings

FOREIGN / INTERNATIONAL PIPELINE
  ibkr_holdings_cash_worker.py   two role-separated Flex pulls a day
                                 (06:00 ET authoritative, 17:30 ET provisional),
                                 refetch-floor dedup, statement-cached
  ibkr_stream_daemon.py          TWS API via IB Gateway + ib_async (scaffold)
  vested_worker.py / dbs_worker.py   Playwright scrape (Vested USD, DBS SGD)
  equity/dbs_ingest.py + dbs_statement.py   DBS weekly holdings-CSV upload path
  foreign_price_worker.py        native-price refresh every minute during US hours
                                 (Finnhub → Twelve Data → yfinance)
  foreign_snapshot_worker.py     periodic foreign position snapshots

PMS PIPELINE
  nuvama_pms_worker.py       Nuvama WealthSpectrum scrape
  icici_pms_worker.py        ICICI Prudential PMS (tax ID + password + emailed OTP)

INCOME & CORPORATE ACTIONS
  dividend_worker.py         yfinance ex-dates × ledger replay → dividend
  corporate_actions.py       split/bonus register + unrecorded-split detection
  equity/broker_pnl_ingest.py + broker_pnl_statement.py   statement parse/import
  reconcile_pnl_statements.py + backfill_from_statements.py   diff and fix

BANK ACCOUNTS
  equity/bank_statements.py  statement parser (PDF/CSV/Excel); balances confirmed
                             on the Bank Accounts page → bank_account / bank_statement

ENRICHMENT & MARKET DATA
  amfi_nav_worker.py        AMFI NAV → nav_history (08:00 IST sweep + hourly
                            --catchup for slow-publishing AMCs)
  mf_metrics_worker.py      CAGR/XIRR, P&L, exposure% for MF holdings
  fx_rates_worker.py        FX rates → fx_rate
  benchmark_worker.py       indices, FX, crypto, treasuries, commodities, ₹ spot
                            metals (every minute; GLOBAL codes are NOT gated on
                            NSE/NYSE hours)
  fred_rates_worker.py      US 30-yr mortgage + 12-mo CD (weekly)
  imf_inflation_worker.py   India/US CPI (weekly)
  wpi_worker.py             India WPI from the publisher direct (weekly)

OPERATIONS
  staleness_monitor.py      seven checks: stale data, broker token health, PMS
                            staleness, hand-entered figures nobody refreshed,
                            live-capture session, live-fill mapping, and
                            holdings-vs-ledger reconciliation
  backup_daily.sh           cyclical single-generation DB dump + uploads tree
  alert.py                  email alerts on worker guards/failures
```

---

## Project structure

```
/var/www/
├── requirements.txt             Pinned Python deps (full venv freeze)
├── graphify-out/                Knowledge graph + Obsidian vault of the codebase
├── uploads/                     Canonical uploads tree (gitignored)
├── mis-portal/                  Backend (FastAPI + workers)
│   ├── main.py                  FastAPI app — all /api/v1 routes + auth
│   ├── .env                     Secrets (chmod 600, not committed)
│   ├── venv/                    API virtualenv (gunicorn runs from here)
│   ├── assistant/               Jarvis AI analyst
│   │   ├── engine.py            Streaming agentic loop over the Claude API
│   │   ├── routing.py           Model/effort selection
│   │   ├── tools.py             DB + quantitative tools exposed to the model
│   │   ├── analytics.py         Portfolio analytics helpers
│   │   ├── prompts.py           System prompts
│   │   └── persistence.py       Conversation/message storage
│   ├── equity/
│   │   ├── equity_sync_worker.py / equity_price_worker.py
│   │   ├── token_refresh_worker.py
│   │   ├── tokens.py / equity_tokens.json   Broker token store (atomic write)
│   │   ├── holdings_source.py   Shared cached holdings fetch (one broker hit,
│   │   │                        many consumers)
│   │   ├── intraday_positions.py   T1/unsettled position handling
│   │   ├── models.py / finmath.py / fx.py / asset_class.py
│   │   ├── symbol_bridge.py / isin_lookup.py   name→ISIN/symbol resolution
│   │   ├── bank_statements.py / dbs_statement.py / dbs_ingest.py
│   │   ├── broker_pnl_statement.py / broker_pnl_ingest.py   P&L statement oracle
│   │   ├── ibkr_stream_sink.py / ibkr_backfill_inception.py
│   │   └── brokers/
│   │       ├── zerodha.py       Kite Connect adapter
│   │       ├── angel_one.py     SmartAPI adapter (token self-heal)
│   │       ├── dhan.py          Dhan adapter (24h token, TOTP)
│   │       ├── ibkr.py          IBKR Flex (statement cache; 1001/1025 safe)
│   │       ├── vested.py / dbs.py   scraped foreign brokers
│   │       └── _feed.py         shared scraped-feed cache helper
│   ├── workers/                 ~90 workers, migrations and one-off tools
│   │   ├── cron_wrapper.py      Shared launcher (NB: captures subprocess output,
│   │   │                        so interactive runs look hung until they finish —
│   │   │                        run workers directly for live logs)
│   │   ├── cas_*.py / cams_trigger_worker.py / gmail_worker.py / oauth_setup.py
│   │   ├── amfi_nav_worker.py / mf_metrics_worker.py / isin_resolver.py
│   │   ├── fx_rates_worker.py / benchmark_worker.py
│   │   ├── fred_rates_worker.py / imf_inflation_worker.py / wpi_worker.py
│   │   ├── equity_snapshot_worker.py / live_trade_daemon.py
│   │   ├── equity_txn_metrics_worker.py / snapshot_xirr_worker.py
│   │   ├── fy_returns_worker.py / portfolio_returns_worker.py
│   │   ├── reconstruct_history.py / manual_positions_worker.py
│   │   ├── dividend_worker.py / corporate_actions.py
│   │   ├── reconcile_pnl_statements.py / backfill_from_statements.py
│   │   ├── reconcile_tradebooks.py / cleanup_phantom_snapshot_trades.py
│   │   ├── foreign_price_worker.py / foreign_snapshot_worker.py
│   │   ├── ibkr_holdings_cash_worker.py / ibkr_stream_daemon.py
│   │   ├── vested_worker.py / dbs_worker.py / _portal_scraper.py
│   │   ├── nuvama_pms_worker.py / icici_pms_worker.py / import_pms_realised.py
│   │   ├── shareindia_fno_worker.py       FnO scraper (recon stage)
│   │   ├── report_generator.py / staleness_monitor.py / alert.py
│   │   ├── backup_daily.sh
│   │   └── db_migrate_*.py      Schema migrations
│   └── deploy/systemd/          Stageable unit files
│
├── iws-portal-frontend/
│   ├── app/
│   │   ├── page.tsx             Login
│   │   ├── layout.tsx           Root layout — mounts the one global nav
│   │   ├── dashboard/           Portfolio overview + analytics + market rail
│   │   ├── equity/              Domestic equity (sector-grouped, broker-scoped)
│   │   ├── foreign-equity/      International holdings (IBKR/Vested/DBS)
│   │   ├── mutual-funds/        MF holdings (CAGR, realised gain, FY returns)
│   │   ├── fno/                 Futures & Options
│   │   ├── pms/                 PMS holdings + realised, per provider
│   │   ├── gold-silver/         Commodities
│   │   ├── unlisted/            Unlisted / startup rounds
│   │   ├── properties/          Property register (docs, floors, images)
│   │   ├── art/  collectibles/  Registers with photo galleries
│   │   ├── bank-accounts/       Statement-fed cash accounts
│   │   ├── realised-gains/      FIFO realised: entity / demat / YoY / dividends
│   │   ├── trades/              Manual trade register (admin)
│   │   ├── manual-data/         Manual asset entry (admin)
│   │   ├── assets/[category]/   Generic page for pageless manual categories
│   │   ├── reports/             Report generation + downloads
│   │   ├── assistant/           Jarvis chat UI
│   │   ├── account/  forgot-password/   Password management
│   │   ├── components/          GlobalNav, NavTabs, EntitySwitcher, MarketRail,
│   │   │                        PrivacyGlass, StickyChrome, PhotoLightbox,
│   │   │                        GalleryAssetGrid, DividendsCard, DragScroll
│   │   └── lib/                 nav.ts, navCoverage.ts, manualCategories.ts,
│   │                            asOf.ts, useMe.ts
│   ├── middleware.ts / proxy.ts CSP nonce + CSP policy
│   └── .env.local              API URL (chmod 600, not committed)
│
└── docs/
    ├── recap-*.md              Architecture recaps
    └── work-log-YYYY-MM-DD.md  Daily work logs
```

### Graph-first navigation

A graphify knowledge graph of the whole codebase lives at `graphify-out/graph.json`, with a browsable Obsidian vault at `graphify-out/obsidian/`. Query it before reading files:

```bash
graphify query "<natural language question>"   # run from /var/www
graphify /var/www --update                     # incremental refresh after changes
```

---

## Database schema

66 tables in PostgreSQL. Grouped by domain (key columns shown):

**Identity & access**
- `pan_group` — tax-filing group: id, name, identifier, description
- `entity` — id, entity_name, pan_group_id, and the entity's own + statement-collection mailbox addresses
- `users` — id, email, password_hash, full_name, entity_id, role, is_active, failed_attempts, locked_until, last_login, token_version
- `password_reset_request` — admin-mediated reset requests (no outbound email)
- `audit_log` — user_id, action, table_name, record_id, old_value, new_value, created_at
- `account` — broker/demat/bank accounts per entity

**Securities, prices & benchmarks**
- `security_master` — id, isin, security_name, security_type, asset_class, currency, amfi_code, exchange, proxy_benchmark_id
- `nav_history` — security_id, nav_date, nav (unique sec+date)
- `security_price_history` — dated equity prices for FY anchors and backfills
- `security_split` / `corporate_action` — split & bonus register (ex-date, ratio, verification source)
- `security_symbol_map` — broker symbol → security resolution
- `fx_rate` — currency pair rates
- `market_benchmark` — code, label, as_of_date, value, prev_close, unit, source
- `security_type_override` / `asset_class_override` — manual classification overrides

**Mutual funds**
- `holding` — entity_id, security_id, folio_number, quantity, avg_cost, cost_basis, current_value, first_invested_date, last_updated_nav, pnl_ytd, pnl_inception, cagr_inception_pct, xirr_inception_pct, exposure_pct, weekly_change, fy_returns
- `mf_transaction` — entity_id, security_id, folio_number, transaction_date, amount, units, nav, balance_units, transaction_type, stamp_duty, source, dedup_key
- `folio_mapping` — folio_number → entity_id, mf_scheme

**Equity**
- `equity_holding` — entity_id, broker, symbol, isin, exchange, sector, asset_class, quantity, avg_cost, cost, current_price, current_market_value, prev_week_value, exposure_pct, pnl_ytd, pnl_inception, returns_*_pct, cagr_inception_pct, xirr_inception_pct, first_invested_date, fy_returns, symbol_override, native-currency columns, and `intraday_qty` / `intraday_avg_cost` / `intraday_value` / `intraday_as_of` for today's unsettled position (unique entity+broker+symbol). **Shared column list:** `_EQUITY_HOLDING_COLS` serves the equity, foreign-equity and gold/silver queries — a column added here but not to `foreign_equity_holding` makes all three tabs render empty.
- `equity_holding_history` — periodic equity value snapshots (FY-start onward)
- `equity_position_snapshot` — intraday position snapshots driving trade detection
- `foreign_equity_holding` (+ `_history`) — international holdings; same metrics plus native-currency columns, currency, fx_rate
- `stock_transaction` — entity_id, security_id, broker, transaction_date, transaction_type, quantity, price, amount, brokerage/stt/charges, total_cost, fx_rate_used, amount_inr, balance_quantity, source, source_ref
- `equity_trade_ledger` — dated per-trade cash flows (native) feeding inception XIRR
- `broker_cash` — per (entity, broker) free cash: balance (INR), balance_native, currency, fx_rate, as_of_date
- `broker_cash_currency` — per-currency detail behind a multi-currency broker balance
- `broker_pnl_statement` / `broker_pnl_line` — imported broker realised-P&L statements (the reconciliation oracle)
- `broker_api_credentials` — broker, entity_id, credentials (jsonb), access_token, token_expiry, is_active, last_synced_at

**Futures & Options**
- `fno_account` / `fno_position` — per-entity FnO book (Share India / Orbis)

**PMS**
- `pms_holding` — entity_id, as_on_date, holding_type, security_name, isin, quantity, avg_cost, cost, current_price, market_value, weight_pct, source (`source` is the provider: `zerodha_pms` / `icici_pms` / `nuvama`)
- `pms_realised` — per-account realised gains ledger. ICICI supplies no cost basis, so XIRR is computed only where one exists.

**Income**
- `dividend` — entity_id, security_id, ex_date, pay_date, quantity, rate_per_share, amount, currency, fy, source, feed, variance_pct
- `dividend_coverage` — per-security derivation coverage/confidence

**Real assets & alternatives**
- `property` — 39 columns: address, nature, tenure, area, RRR, purchase price + costs, sale price + costs, derived capital gain, two valuations, sold flags
- `property_detail` / `property_floor` / `property_document` / `property_image` / `property_owner` / `property_entity` / `property_nature` / `property_nature_type`
- `art_detail` — artwork register detail
- `unlisted_round` / `unlisted_event` — priced rounds and cap-table events

**Other assets / manual entry**
- `manual_input` — entity_id, category, label, cost, current_value, prev_week_value, currency, fx_rate, raw_amount, inception_date. **Contract: `cost` and `current_value` are always INR**; native amounts go in `raw_amount` + `fx_rate`.
- `manual_entry`, `manual_valuation`, `manual_attachment` — manual records, valuations and uploaded files
- `ppf_transaction` — entity_id, financial_year, contribution_date, principal_amount, interest_rate, interest_credited, closing_balance
- `cash_ledger` — entity_id, account_id, balance_date, balance, currency, fx_rate, balance_inr, source
- `bank_account` / `bank_statement` — cash-only accounts (one entity each) and the uploaded statements that feed them
- `external_cashflow` — entity_id, flow_date, flow_type (DEPOSIT/WITHDRAWAL/DIVIDEND/INTEREST), amount_native, currency (drives per-entity XIRR)

**Analytics snapshots**
- `daily_snapshot` — entity_id, security_id, snapshot_date, quantity, nav, market_value_inr, opening_value_inr, todays_pnl_inr/pct, inception_pnl_inr/pct, cost_basis_inr
- `portfolio_summary` — entity_id, summary_date, asset_class, total_invested_inr, current_value_inr, inception/today P&L, weight_pct
- `portfolio_returns` — entity_id, as_of_date, xirr_pct, deposits/withdrawals/income_inr, current_value_inr, coverage (full/partial)

**Operations**
- `ingestion_run` — run_type, run_date, status, records_processed, records_failed, error_message, timings. Note: equity workers do **not** write here — their health shows via `broker_api_credentials.last_synced_at` + `equity_holding.updated_at`.
- `reconciliation_ticket` — data-reconciliation issues
- `generated_report` — report_type, entity_id, filename, filepath, as_of_date, generated_by

**AI assistant**
- `assistant_conversation` — user_id, title, archived_at
- `assistant_message` — conversation messages (role, content, tool calls, charts)

---

## Schedule

Server is UTC; IST = UTC+5:30.

### systemd timers

| When | Unit | Worker |
|---|---|---|
| every minute (market-hours guarded) | `mis-portal-equity-price.timer` | domestic LTP + light holdings refresh |
| every minute (US-hours guarded) | `mis-portal-foreign-price.timer` | foreign equity prices |
| Mon–Fri 03:45–09:45 UTC hourly | `mis-portal-equity-snapshot.timer` | intraday snapshots + trade detection |
| Mon–Fri every 2h | `mis-portal-foreign-snapshot.timer` | Vested position snapshots |
| Mon–Fri 06:00 + 17:30 ET | `mis-portal-ibkr-flex.timer` | IBKR Flex (authoritative + provisional) |
| Mon–Fri 03:35 UTC | `mis-portal-live-trade-start.timer` | start live-trade daemons |
| daily 00:50 UTC | `mis-portal-live-trade-stop.timer` | stop daemons **before** the 01:00 token refresh |
| on demand | `mis-portal-ibkr-stream@.service` | IB Gateway TWS streaming (per login) |

A live-trade daemon that outlives the 01:00 UTC token refresh 403-loops all session while systemd still reports `active` — hence the 00:50 stop and the auth-fatal exit path.

### cron (SAdmin, via `cron_wrapper.py`)

| UTC | IST | Worker | Log |
|---|---|---|---|
| every minute | — | `benchmark_worker.py` | `mis-portal-benchmark.log` |
| :15 hourly | — | `amfi_nav_worker.py --catchup` | `mis-portal-amfi-worker.log` |
| 00:30 | 06:00 | `fx_rates_worker.py` | `mis-portal-fx-worker.log` |
| 00:30 (Mon/Wed/Fri) | 06:00 | `nuvama_pms_worker.py` | `mis-portal-pms.log` |
| 01:00 | 06:30 | `equity/token_refresh_worker.py` | `mis-portal-equity-token.log` |
| 01:05 (Mon–Fri) | 06:35 | `vested_worker.py` | `mis-portal-vested.log` |
| 01:30 | 07:00 | `equity/equity_sync_worker.py` | `mis-portal-equity-sync.log` |
| 02:00 | 07:30 | `equity_txn_metrics_worker.py --commit` | `mis-portal-equity-metrics.log` |
| 02:05 | 07:35 | `portfolio_returns_worker.py --commit` | `mis-portal-equity-metrics.log` |
| 02:10 | 07:40 | `snapshot_xirr_worker.py --commit` | `mis-portal-equity-metrics.log` |
| 02:30 | 08:00 | `amfi_nav_worker.py` (chains MF metrics) | `mis-portal-amfi-worker.log` |
| 02:45 | 08:15 | `mf_metrics_worker.py` (safety net) | `mis-portal-mf-metrics.log` |
| 03:00 (Mon–Fri) | 08:30 | `icici_pms_worker.py` | `mis-portal-icici-pms.log` |
| 04:30 (Mon–Fri) | 10:00 | `equity_sync_worker.py` (Dhan catch-up) | `mis-portal-equity-sync.log` |
| 06:30 (Mon–Fri) | 12:00 | `equity_txn_metrics_worker.py --commit` | `mis-portal-equity-metrics.log` |
| 10:30 (Mon–Fri) | 16:00 | `equity_txn_metrics_worker.py --commit` | `mis-portal-equity-metrics.log` |
| 11:00 (Mon–Fri) | 16:30 | `broker_txn_sync_worker.py --commit` | `mis-portal-broker-txn-sync.log` |
| 12:00 (Mon–Fri) | 17:30 | `staleness_monitor.py` | `mis-portal-staleness.log` |
| 13:30 (Fri) | 19:00 | `fred_rates_worker.py --commit` | `mis-portal-fred-rates.log` |
| 13:45 (Fri) | 19:15 | `imf_inflation_worker.py --commit` | `mis-portal-imf-inflation.log` |
| 13:50 (Fri) | 19:20 | `wpi_worker.py --commit` | `mis-portal-imf-inflation.log` |
| 17:30 | 23:00 | `cas_automation_worker.py` | `mis-portal-cas-auto.log` |
| 21:30 | 03:00 | `backup_daily.sh` | `~/backups/mis-portal/backup.log` |

> **The `/var/log` trap.** Creating a new `/var/log/mis-portal-*.log` file needs root, and cron redirecting into a file it cannot create fails *silently* — the worker never runs and nothing is logged. New workers should reuse an existing log (as `wpi_worker.py` shares the inflation log) unless the file has been created with the right ownership first.

---

## Broker & provider credentials

Per-entity credentials live in `mis-portal/.env`, keyed by entity code — `{PROVIDER}_{E}_{FIELD}`, where `{E}` is the entity's short code. Run `grep -o '^[A-Z_]*=' mis-portal/.env` on the server to see which are actually configured.

### Angel One (SmartAPI)
| Variable | Where to find |
|---|---|
| `ANGEL_{E}_API_KEY` | My Profile → Apps in the Angel One web portal |
| `ANGEL_{E}_CLIENT_ID` | Angel One login username |
| `ANGEL_{E}_PASSWORD` | 4-digit MPIN (not the login password) |
| `ANGEL_{E}_TOTP_SECRET` | Base32 TOTP secret from app setup (not the 6-digit code) |

Angel One tags NSE EQ-series symbols with a `-EQ` suffix. That is the same stock as the plain symbol — the ISIN-less order stream once forked a second `security_master` row per name. The live daemon now resolves ISIN (stripping only `-EQ`) before insert.

### Zerodha (Kite Connect)
| Variable | Where to find |
|---|---|
| `ZERODHA_{E}_API_KEY` / `_API_SECRET` | Kite Connect developer console |
| `ZERODHA_{E}_CLIENT_ID` | Zerodha login username |
| `ZERODHA_{E}_PASSWORD` | Zerodha account password |
| `ZERODHA_{E}_TOTP_SECRET` | Base32 TOTP secret (not the 6-digit code) |

An `api_secret` must be exactly 32 characters — a stray typed character produces an opaque "Invalid checksum" and the length check is the fastest tell.

### Dhan
24-hour access token, auto-renewed daily via `/RenewToken`; on renewal failure it regenerates headlessly from PIN + TOTP (`equity/brokers/dhan.py`).

| Variable | Where to find |
|---|---|
| `DHAN_{E}_CLIENT_ID` | Dhan account / client ID |
| `DHAN_{E}_API_KEY` / `_API_SECRET` | dhanhq.co developer portal |
| `DHAN_{E}_PIN` | Dhan login PIN (for headless TOTP generation) |
| `DHAN_{E}_TOTP_SECRET` | Base32 TOTP secret (not the 6-digit code) |
| `DHAN_{E}_ACCESS_TOKEN` | Bootstrap once from web.dhan.co → Access DhanHQ APIs; auto-renewed in place |

Live LTP is read from the Dhan holdings feed (`get_holdings().lastTradedPrice`) rather than a separate quote call, and `dhanhq` is pinned because its price-fetch surface changes between releases. Dhan's order-update WebSocket sends **camelCase** keys despite the docs saying PascalCase — the wrong casing yields a healthy-looking socket with "events N, fills 0". Its Part-Traded quantity is cumulative, not incremental.

### Interactive Brokers (Flex Web Service)
A token + saved Flex query (Open Positions **+** Cash Report in one XML). Multiple logins per entity roll up via numbered prefixes (`IBKR_{E}_2_*`). The service throttles hard — same-query regeneration returns `1001`, accumulated failures escalate to a `1025` lockout, and the only cure is silence — so `ibkr.py` caches the statement (in-process + on-disk fallback), the worker makes at most one hit per login, and a 6-hour refetch floor prevents regeneration storms.

| Variable | Where to find |
|---|---|
| `IBKR_{E}_FLEX_TOKEN` | Client Portal → Settings → Reporting → Flex Web Service |
| `IBKR_{E}_QUERY_ID` | saved Activity Flex Query (Open Positions + Cash Report, XML) |
| `IBKR_{E}_TRADES_QUERY_ID` | optional; Trades-section query for the inception backfill |
| `IBKR_{E}_BASE_CURRENCY` | optional; account base currency for cash (default USD) |
| `IBKR_SYNC_PAUSED` | kill-switch — `1` keeps the daily sync + cash refresh off IBKR |

Flex reports only accounts that have position reporting authorised; an account can hold positions and still return zero `OpenPositions` through a given token. Verify against the client portal before concluding a position was sold.

### Vested (US) / DBS (SGD)
Playwright portal scrapers (no public API). `VESTED_{E}_USERNAME` / `_PASSWORD` / `_PIN`; `DBS_{E}_USERNAME` / `_PASSWORD`. DBS additionally supports a weekly holdings-CSV upload with preview + commit. Holdings land in `foreign_equity_holding` in native currency, FX-converted to INR.

### PMS — Nuvama WealthSpectrum / ICICI Prudential
`NUVAMA_PMS_BASE_URL`, `NUVAMA_PMS_REPORT`, `NUVAMA_PMS_OUTPUT_FORMAT`, and per-entity `NUVAMA_PMS_{E}_USERNAME` / `_PASSWORD` / `_OWNER`. ICICI logs in with tax ID + password + an emailed OTP, read from the collector inbox with `in:anywhere` (forwarded OTP mail usually lands in Spam).

### Share India uTrade (FnO)
`SHAREINDIA_{E}_USERNAME` / `_PASSWORD` plus emailed OTP. Until `SHAREINDIA_SCHEMA_READY=1` the worker performs exactly **one** login per run (each attempt burns an OTP and repeats risk a lockout) and captures screenshots/HTML/XHR for parser development.

### Foreign-equity market data
Independent price feed for `foreign_equity_holding`, tried in order: Finnhub → Twelve Data → yfinance. `FINNHUB_API_KEY` (free tier, US real-time) and optional `TWELVEDATA_API_KEY`; yfinance is the no-key fallback. Finnhub free is US-only, so non-US listings (e.g. an LSE ETF) fall through to yfinance via `symbol_override`.

---

## Gmail OAuth setup (one-time per inbox)

```bash
cd mis-portal/workers
python oauth_setup.py --token gmail_token_central.json
# Opens browser → authorise the collector inbox → token saved
```

**One shared token serves both CAS reads and alert sends.** Re-authorise only as the collector account — authorising as anyone else points the CAS collector at the wrong inbox. All entities forward their CAS mail into this one inbox, so a CAS timeout is a real failure, never a missing forward.

---

## AI analyst (Jarvis)

A manual, streaming agentic loop over the Claude Messages API (`assistant/engine.py`). The model emits tool *intent*; the backend executes each client tool against a DB connection with server-side scoping — **the model is never the security boundary**. Anthropic-hosted server tools (`web_search`, `code_execution`) stream back inline with citations, and `render_chart` returns structured chart specs persisted alongside the message. Conversations live in `assistant_conversation` / `assistant_message`; model and reasoning effort are chosen per request in `assistant/routing.py`.

---

## Security model

| Control | Implementation |
|---|---|
| Authentication | JWT (2h TTL), httpOnly + Secure + SameSite=Strict cookie |
| Token revocation | Redis blacklist (SHA-256 hash of token), fail-closed, plus a per-user `token_version` |
| Role enforcement | Live DB role check on every request (`_live_role()`), not the JWT claim |
| Entity scoping | Every data route scoped server-side; admin-only routes 403 for members |
| Password storage | bcrypt, max 72 bytes enforced |
| Password lifecycle | Self-service change + admin-mediated reset via `password_reset_request` |
| Brute force | 5-attempt lockout, generic 401 response |
| Rate limiting | `slowapi`, keyed on `CF-Connecting-IP` |
| Uploads | Extension/MIME whitelist, 200 MB cap, streamed to disk, served via X-Accel-Redirect |
| CSP | Policy in `proxy.ts`, per-request nonce injected by `middleware.ts` |
| Audit | `audit_log` records mutating actions (old/new value) |
| Secrets | `.env` / `.env.local` chmod 600; tax identifiers, mailbox addresses and the entity roster live in the DB, never in the repo or env |
| Screenshots | Scraper screenshot dirs chmod 700, each file chmod 600 |
| Broker tokens | `equity_tokens.json` chmod 600; atomic write (tmp → rename) |

> The GitHub repository is **public**. Never commit secrets, and keep exploit detail and the entity-visibility model out of commit messages.

---

## Operations

### Services

```bash
sudo systemctl status mis-portal              # FastAPI backend
sudo systemctl status iws-frontend            # Next.js frontend
sudo systemctl list-timers 'mis-portal-*'     # all worker timers
sudo systemctl restart mis-portal             # restart after config changes
```

Logs: `journalctl -u mis-portal -f` · per-worker logs under `/var/log/mis-portal-*.log`

### Running a worker by hand

```bash
cd /var/www/mis-portal && .venv/bin/python workers/<worker>.py
```

Run workers **directly**, not through `cron_wrapper.py` — the wrapper buffers subprocess output, so an interactive run looks hung until it finishes.

### Monitoring

`staleness_monitor.py` emails on seven independent checks. Several deliberate false-positive guards exist inside it — a market-hours guard (post-close idle time is not a stalled price worker), a log-based live-daemon session check (daemons stop before the monitor runs, so systemd state is the wrong signal), and staleness judged on `as_of_date` rather than `updated_at`. Do not remove them.

### Backups

`backup_daily.sh` runs at 21:30 UTC and writes a single, atomically overwritten generation of the database dump plus the uploads tree to `~/backups/mis-portal`. It is **same-disk only** — there is no offsite copy.
