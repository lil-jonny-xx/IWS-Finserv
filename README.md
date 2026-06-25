# IWS Finserv MIS Portal

Internal portfolio management and analytics system for IWS Finserv and its linked investment entities. Automates the full data pipeline — CAMS CAS statement ingestion, domestic + international broker sync, PMS scraping, NAV/FX/market-data enrichment, and benchmark tracking — and presents consolidated holdings, P&L, and analytics across every asset class through a Next.js dashboard with a built-in AI analyst.

**Entities (7, across 4 PAN groups):**

| PAN group | Entities |
|---|---|
| PAN 1 | DHR (***REMOVED***), ADR (***REMOVED***), IWS (IWS Finserv) |
| PAN 2 | HHR (***REMOVED***), IWS Fincorp |
| PAN 3 | SDR (***REMOVED***) |
| PAN 4 | Rajani Corp |

PAN numbers and CAS emails live in the database (`pan_group.pan_number`, `entity.cas_email`), never in `.env`.

---

## What it does

- **Mutual funds** — automated nightly ingestion from CAMS CAS PDFs via a central Gmail inbox; parsed per entity by folio holder-name matching; CAGR/XIRR, realized & unrealized P&L, exposure
- **Equity (domestic)** — daily holdings sync from Angel One (SmartAPI), Zerodha (Kite Connect), and Dhan; live LTP every minute during market hours; FIFO transaction ledger driving inception XIRR/CAGR; per-(entity,broker) free cash
- **Foreign equity** — international brokers (Interactive Brokers Flex, Vested, DBS) in their native currency, FX-converted to INR; prices refreshed from an independent market-data feed (Finnhub → Twelve Data → yfinance) decoupled from the broker sync; shown on its own page so the Equity page stays India-only
- **PMS** — Nuvama WealthSpectrum discretionary portfolios scraped (Playwright) into `pms_holding` / `pms_realised`
- **Bank accounts** — cash-only accounts whose balances are fed by uploaded statements (PDF/CSV/Excel, parse-then-confirm) or manual entry; fold into the dashboard CASH bucket
- **Other assets** — manual inputs (PPF, cash balances, fixed deposits, unlisted/alternative holdings) folded into the same asset-class buckets
- **Analytics** — today's P&L, inception P&L, YTD P&L, CAGR/XIRR, asset-class allocation, weekly delta, FX-adjusted values; per-entity money-weighted XIRR from real external cash flows (`portfolio_returns`). Annualised CAGR/XIRR are shown only once a holding is held ≥1 year — below that the absolute return is shown instead
- **Benchmarks** — live index/benchmark ticker, per-security proxy benchmarks
- **Reports** — on-demand generated reports (per entity / consolidated) with downloadable artifacts; include broker & bank cash, PMS, and real-estate lines
- **AI analyst (Jarvis)** — streaming agentic assistant over the Claude Messages API; answers natural-language questions using entity-scoped DB tools plus Anthropic-hosted web search & code execution
- **Multi-entity** — admin view across all PANs/entities; scoped per-entity view for non-admin users
- **Security** — JWT auth, Redis token blacklist, bcrypt passwords, per-request CSP nonce, rate limiting, account lockout, audit log

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI (Python 3.12), Gunicorn + Uvicorn workers |
| Frontend | Next.js 16, React 19, TypeScript 5, Tailwind CSS 4 |
| Database | PostgreSQL 16 |
| AI | Claude Messages API (`anthropic` SDK) — streaming tool-use loop |
| Market data | AMFI (MF NAV), Finnhub / Twelve Data / yfinance (foreign equity), broker LTP (domestic) |
| Auth | JWT (2h TTL) + Redis blacklist + httpOnly cookies |
| Automation | Playwright (Chromium), playwright-stealth, Xvfb (CAMS, Nuvama PMS, Vested, DBS) |
| Email | Gmail API (OAuth 2.0) |
| Reverse proxy | Nginx + Cloudflare |

---

## Install

Python deps are fully pinned at the repo root for reproducible deploys:

```bash
/var/www/.venv/bin/pip install -r requirements.txt   # backend + workers
cd iws-portal-frontend && npm ci && npm run build      # frontend
```

Regenerate the pin after a dependency change with `/var/www/.venv/bin/pip freeze > requirements.txt`. Keep `dhanhq` pinned — an unpinned upgrade previously broke the equity price worker's Dhan adapter silently.

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
Nginx :443  (SSL via Cloudflare origin cert; HSTS, CSP, Referrer-Policy)
  │
  ├── /        ──► Next.js :3000  (iws-frontend.service)
  │                middleware.ts injects a per-request CSP nonce
  │                Pages: / (login)  /dashboard  /equity  /foreign-equity
  │                       /mutual-funds  /pms  /bank-accounts
  │                       /realised-gains  /manual-data  /benchmarks
  │                       /reports  /assistant
  │
  └── /api/    ──► Gunicorn + Uvicorn :8000  (mis-portal.service, 3 workers)
                   FastAPI app — mis-portal/main.py
                   Routes under /api/v1: auth, me, entities, overview,
                     holdings (MF), equity, foreign-equity, pms,
                     bank-accounts, transactions, realised-gains,
                     manual-inputs, benchmarks, fx-rates, dhan, reports,
                     assistant, health
```

### Data pipelines

All workers are launched by `workers/cron_wrapper.py` (shared venv + logging entrypoint). The server runs in **UTC**; IST equivalents are shown in the schedule below.

```
NIGHTLY CAS PIPELINE
  cas_automation_worker.py  (orchestrator)
    ├── cams_trigger_worker.py   Playwright fills the CAMS CAS form per entity
    ├── gmail_worker.py          Gmail API polls ***REMOVED*** for the PDF
    └── cas_parser_worker.py     PyMuPDF + casparser → upsert holdings/txns

EQUITY PIPELINE (domestic)
  token_refresh_worker.py   refreshes broker OAuth/TOTP tokens
  equity_sync_worker.py     syncs holdings + broker cash from Angel One, Zerodha, Dhan
                            (and, when configured, the international brokers below)
  equity_price_worker.py    live LTP every minute (systemd timer; self-guards
                            market hours 09:15–15:30 IST)
  stock_transaction_worker.py / broker_txn_sync_worker.py   stock transactions
  import_tradebooks_multi.py + symbol_bridge.py   tradebook import → stock_transaction
  equity_txn_metrics_worker.py   FIFO inception XIRR/CAGR/YTD from transactions
  portfolio_returns_worker.py    per-entity money-weighted XIRR from external_cashflow

FOREIGN / INTERNATIONAL PIPELINE
  vested_worker.py / dbs_worker.py   Playwright scrape (Vested USD, DBS SGD)
  brokers/ibkr.py            Interactive Brokers Flex Web Service (positions + cash;
                            holdings/cash fetched via ibkr_holdings_cash_worker.py,
                            statement-cached, throttle-paced — see brokers section)
  foreign_price_worker.py    independent native-price refresh during US hours
                            (Finnhub → Twelve Data → yfinance), US-market-hours guard

PMS PIPELINE
  nuvama_pms_worker.py       Nuvama WealthSpectrum scrape → pms_holding / pms_realised

BANK ACCOUNTS
  equity/bank_statements.py  statement parser (PDF/CSV/Excel); balances confirmed via
                            the Bank Accounts page → bank_account / bank_statement

ENRICHMENT WORKERS
  amfi_nav_worker.py        AMFI NAV CSV → nav_history (twice nightly)
  mf_metrics_worker.py      CAGR/XIRR, P&L, exposure% for MF holdings
  fx_rates_worker.py        FX rates → fx_rate
  benchmark_worker.py       market benchmark/index values (every minute)
```

---

## Project structure

```
/var/www/
├── requirements.txt             Pinned Python deps (full venv freeze)
├── mis-portal/                  Backend (FastAPI + workers)
│   ├── main.py                  FastAPI app — all /api/v1 routes + auth
│   ├── .env                     Secrets (chmod 600, not committed)
│   ├── requirements.txt         Legacy partial list (root file is authoritative)
│   ├── assistant/               Jarvis AI analyst
│   │   ├── engine.py            Streaming agentic loop over the Claude API
│   │   ├── routing.py           Model/effort selection
│   │   ├── tools.py             Entity-scoped DB tools exposed to the model
│   │   ├── analytics.py         Portfolio analytics helpers
│   │   ├── prompts.py           System prompts
│   │   └── persistence.py       Conversation/message storage
│   ├── equity/
│   │   ├── equity_sync_worker.py
│   │   ├── equity_price_worker.py   Live LTP (DhanAdapter reads holdings feed)
│   │   ├── token_refresh_worker.py
│   │   ├── tokens.py / equity_tokens.json   Broker token store (atomic write)
│   │   ├── models.py            EquityHolding dataclass
│   │   ├── finmath.py           XIRR/IRR solver
│   │   ├── fx.py                FX-rate lookup/conversion helper
│   │   ├── symbol_bridge.py / isin_lookup.py   name→ISIN/symbol resolution
│   │   ├── bank_statements.py   bank-statement parser (PDF/CSV/Excel)
│   │   ├── ibkr_backfill_inception.py   one-time IBKR trades backfill (shelved)
│   │   ├── equity_history_backfill.py
│   │   ├── zerodha_tradebook_import.py
│   │   └── brokers/
│   │       ├── zerodha.py       Kite Connect adapter
│   │       ├── angel_one.py     SmartAPI adapter (token self-heal: tokens.json
│   │       │                    first, reauth+retry on intraday session death)
│   │       ├── dhan.py          Dhan adapter (24h token, TOTP auto-renew)
│   │       ├── ibkr.py          Interactive Brokers Flex (statement cache + on-disk
│   │       │                    fallback; 1001/1025 throttle-safe)
│   │       ├── vested.py        Vested (US, USD) — scraped
│   │       ├── dbs.py           DBS (SGD) — scraped
│   │       └── _feed.py         shared scraped-feed cache helper
│   ├── workers/
│       ├── cron_wrapper.py      Shared launcher for all cron jobs
│       ├── cas_automation_worker.py / cams_trigger_worker.py
│       ├── gmail_worker.py / oauth_setup.py
│       ├── cas_parser_worker.py
│       ├── amfi_nav_worker.py / nav_history_backfill.py / isin_resolver.py
│       ├── mf_metrics_worker.py
│       ├── fx_rates_worker.py / fx_backfill_worker.py
│       ├── benchmark_worker.py
│       ├── stock_transaction_worker.py / broker_txn_sync_worker.py
│       ├── import_tradebook.py / import_tradebooks_multi.py / import_ledgers.py
│       ├── equity_txn_metrics_worker.py    FIFO inception XIRR/CAGR/YTD
│       ├── portfolio_returns_worker.py     per-entity money-weighted XIRR
│       ├── foreign_price_worker.py         foreign equity prices (Finnhub/TwelveData/yfinance)
│       ├── ibkr_holdings_cash_worker.py    paced IBKR holdings+cash (one Flex hit/login)
│       ├── vested_worker.py / dbs_worker.py / _portal_scraper.py   foreign scrapers
│       ├── nuvama_pms_worker.py / import_pms_realised.py   PMS
│       ├── report_generator.py
│       ├── bootstrap_dhan_holdings.py / manual_cas_retrigger.py
│       ├── alert.py             Email alerts on worker guards/failures
│       ├── seed_users.py
│       └── db_migrate_*.py      Schema migrations
│   └── deploy/systemd/          Stageable unit files (foreign-price timer + README)
│
├── iws-portal-frontend/
│   ├── app/
│   │   ├── page.tsx             Login
│   │   ├── dashboard/           Portfolio overview + analytics
│   │   ├── equity/              Domestic equity holdings (sector-grouped)
│   │   ├── foreign-equity/      International holdings (IBKR/Vested/DBS, multi-ccy)
│   │   ├── mutual-funds/        MF holdings (CAGR, realized gain, filters)
│   │   ├── pms/                 PMS (Nuvama) holdings + realised
│   │   ├── bank-accounts/       Statement-fed cash accounts
│   │   ├── realised-gains/      Realized gains ledger
│   │   ├── manual-data/         Manual asset entry (PPF, cash, etc.)
│   │   ├── benchmarks/          Benchmark dashboard
│   │   ├── reports/             Report generation + downloads
│   │   ├── assistant/           Jarvis chat UI
│   │   ├── BenchmarkTicker.tsx / IdleTimeout.tsx
│   │   └── middleware.ts        CSP nonce injection
│   └── .env.local              API URL (chmod 600, not committed)
│
└── docs/
    ├── recap-*.md              Architecture recaps
    └── work-log-YYYY-MM-DD.md  Daily work logs
```

---

## Database schema

39 tables in PostgreSQL. Grouped by domain (key columns shown):

**Identity & access**
- `pan_group` — id, pan_name, pan_number, description
- `entity` — id, entity_name, pan_group_id, email, cas_email
- `users` — id, email, password_hash, full_name, entity_id, role, is_active, failed_attempts, locked_until, last_login
- `audit_log` — user_id, action, table_name, record_id, old_value, new_value, created_at
- `account` — broker/demat/bank accounts per entity

**Securities, prices & benchmarks**
- `security_master` — id, isin, security_name, security_type, asset_class, currency, amfi_code, exchange, proxy_benchmark_id
- `nav_history` — security_id, nav_date, nav  (unique sec+date)
- `fx_rate` — currency pair rates
- `market_benchmark` — code, label, as_of_date, value, unit, source
- `security_type_override` — manual asset-class/type overrides

**Mutual funds**
- `holding` — id, entity_id, security_id, folio_number, quantity, avg_cost, cost_basis, current_value, first_invested_date, last_updated_nav, pnl_ytd, pnl_inception, cagr_inception_pct, xirr_inception_pct, exposure_pct, weekly_change
- `mf_transaction` — entity_id, security_id, folio_number, transaction_date, amount, units, nav, balance_units, transaction_type, stamp_duty, source
- `folio_mapping` — folio_number → entity_id, mf_scheme

**Equity**
- `equity_holding` — id, entity_id, broker, symbol, isin, exchange, sector, quantity, avg_cost, cost, current_price, current_market_value, prev_week_value, exposure_pct, pnl_ytd, pnl_inception, returns_*_pct, cagr_inception_pct, first_invested_date, symbol_override, angel_one_token  (unique entity+broker+symbol)
- `equity_holding_history` — periodic equity value snapshots (FY-start onward)
- `foreign_equity_holding` — international holdings; same metrics as `equity_holding` plus native-currency columns (avg_cost_native, cost_native, current_price_native, current_market_value_native), currency, fx_rate, symbol_override  (unique entity+broker+symbol)
- `foreign_equity_holding_history` — foreign holding snapshots (native + INR)
- `stock_transaction` — entity_id, security_id, transaction_date, transaction_type, quantity, price, amount, brokerage/stt/charges, total_cost, fx_rate_used, amount_inr, balance_quantity, source
- `equity_trade_ledger` — dated per-trade cash flows (native) feeding inception XIRR (IBKR backfill)
- `broker_cash` — per (entity, broker) free cash: balance (INR), balance_native, currency, fx_rate, as_of_date
- `broker_api_credentials` — broker, entity_id, credentials (jsonb), access_token, token_expiry, is_active, last_synced_at

**PMS (Nuvama)**
- `pms_holding` — entity_id, scheme, security, quantity, cost, current_value, cash, metrics  (scraped from WealthSpectrum)
- `pms_realised` — entity_id, realised gains ledger for the PMS account

**Other assets / manual entry**
- `manual_input` — entity_id, category, label, cost, current_value, prev_week_value, currency, fx_rate, inception_date
- `manual_entry`, `manual_valuation` — manual holding/valuation records
- `ppf_transaction` — entity_id, financial_year, contribution_date, principal_amount, interest_rate, interest_credited, closing_balance
- `cash_ledger` — entity_id, account_id, balance_date, balance, currency, fx_rate, balance_inr, source
- `bank_account` — entity_id, bank_name, account_type, currency, balance, fx_rate, as_of_date  (cash-only, statement-fed; one entity per account)
- `bank_statement` — uploaded statement metadata + parse/confirm status feeding `bank_account`
- `external_cashflow` — entity_id, flow_date, flow_type (DEPOSIT/WITHDRAWAL/DIVIDEND/INTEREST), amount_native, currency  (drives per-entity XIRR)

**Analytics snapshots**
- `daily_snapshot` — entity_id, security_id, snapshot_date, quantity, nav, market_value_inr, opening_value_inr, todays_pnl_inr/pct, inception_pnl_inr/pct, cost_basis_inr
- `portfolio_summary` — entity_id, summary_date, asset_class, total_invested_inr, current_value_inr, inception/today P&L, weight_pct
- `portfolio_returns` — entity_id, as_of_date, xirr_pct, deposits/withdrawals/income_inr, current_value_inr, coverage (full/partial)  (money-weighted return from external_cashflow)

**Operations**
- `ingestion_run` — run_type, run_date, status, records_processed, records_failed, error_message, started_at, completed_at  (worker run log; note: equity workers do **not** write here — their health shows via `broker_api_credentials.last_synced_at` + `equity_holding.updated_at`)
- `reconciliation_ticket` — data-reconciliation issues
- `generated_report` — report_type, entity_id, filename, filepath, as_of_date, generated_by

**AI assistant**
- `assistant_conversation` — user_id, title, scope_entity_id, archived_at
- `assistant_message` — conversation messages (role, content, tool calls)

---

## Schedule (server is UTC; IST = UTC+5:30)

| UTC | IST | Worker | Log |
|---|---|---|---|
| every 1 min | — | `equity_price_worker.py` (systemd timer, market-hours guarded) | `/var/log/mis-portal-equity-price.log` |
| every 1 min | — | `benchmark_worker.py` | `/var/log/mis-portal-benchmark.log` |
| 00:30 | 06:00 | `fx_rates_worker.py` | `/var/log/mis-portal-fx-worker.log` |
| 00:30 (Mon/Wed/Fri) | 06:00 | `nuvama_pms_worker.py` (randomised order + delays) | `/var/log/mis-portal-pms.log` |
| 01:00 | 06:30 | `equity/token_refresh_worker.py` | `/var/log/mis-portal-equity-token.log` |
| 01:05 (Mon/Wed/Fri) | 06:35 | `vested_worker.py` (USD scrape) | `/var/log/mis-portal-vested.log` |
| 01:30 | 07:00 | `equity/equity_sync_worker.py` (IBKR leg gated by `IBKR_SYNC_PAUSED`) | `/var/log/mis-portal-equity-sync.log` |
| 02:00 | 07:30 | `equity_txn_metrics_worker.py` (FIFO XIRR/CAGR/YTD) | `/var/log/mis-portal-equity-metrics.log` |
| 02:05 | 07:35 | `portfolio_returns_worker.py` (per-entity XIRR) | `/var/log/mis-portal-equity-metrics.log` |
| 11:00 (Mon–Fri) | 16:30 | `broker_txn_sync_worker.py` (live broker txns) | `/var/log/mis-portal-broker-txn-sync.log` |
| 16:30 (Mon–Fri) | 22:00 | `stock_transaction_worker.py` | `/var/log/mis-portal-stock-txn.log` |
| 16:30 / 18:00 | 22:00 / 23:30 | `amfi_nav_worker.py` (twice) | `/var/log/mis-portal-amfi-worker.log` |
| 16:45 / 18:15 | 22:15 / 23:45 | `mf_metrics_worker.py` (twice) | `/var/log/mis-portal-mf-metrics.log` |
| 17:30 | 23:00 | `cas_automation_worker.py` | `/var/log/mis-portal-cas-auto.log` |

Equity-price runs via `mis-portal-equity-price.timer`; the rest are SAdmin cron jobs invoked through `workers/cron_wrapper.py`. `foreign_price_worker.py` is staged as `mis-portal-foreign-price.timer` (`deploy/systemd/`, every minute, self-guards US market hours) but not yet installed. **IBKR is not on any schedule** — it runs manually via `ibkr_holdings_cash_worker.py` and stays off the daily sync while `IBKR_SYNC_PAUSED=1`, pending a per-token request-timing design.

---

## Broker API credentials

Per-entity credentials live in `mis-portal/.env`, keyed by entity code (e.g. `ANGEL_HHR_*`).

### Angel One (SmartAPI)
| Variable | Where to find |
|---|---|
| `ANGEL_{E}_API_KEY` | My Profile → Apps in the Angel One web portal |
| `ANGEL_{E}_CLIENT_ID` | Angel One login username (e.g. `***REMOVED***`) |
| `ANGEL_{E}_PASSWORD` | 4-digit MPIN (not the login password) |
| `ANGEL_{E}_TOTP_SECRET` | Base32 TOTP secret from app setup (not the 6-digit code) |

### Zerodha (Kite Connect)
| Variable | Where to find |
|---|---|
| `ZERODHA_{E}_API_KEY` | Kite Connect developer console |
| `ZERODHA_{E}_API_SECRET` | Kite Connect developer console |
| `ZERODHA_{E}_CLIENT_ID` | Zerodha login username |
| `ZERODHA_{E}_PASSWORD` | Zerodha account password |
| `ZERODHA_{E}_TOTP_SECRET` | Base32 TOTP secret (not the 6-digit code) |

### Dhan
24-hour access token, auto-renewed daily via `/RenewToken`; on renewal failure it regenerates headlessly from PIN + TOTP (see `equity/brokers/dhan.py`). Entities: HHR, Rajani Corp.

| Variable | Where to find |
|---|---|
| `DHAN_{E}_CLIENT_ID` | Dhan account / client ID |
| `DHAN_{E}_API_KEY` | App ID from the dhanhq.co developer portal |
| `DHAN_{E}_API_SECRET` | App secret from the dhanhq.co developer portal |
| `DHAN_{E}_PIN` | Dhan login PIN (for headless TOTP generation) |
| `DHAN_{E}_TOTP_SECRET` | Base32 TOTP secret (not the 6-digit code) |
| `DHAN_{E}_ACCESS_TOKEN` | Bootstrap once from web.dhan.co → Access DhanHQ APIs; thereafter auto-renewed in-place |

Live LTP is read from the Dhan holdings feed (`get_holdings().lastTradedPrice`) rather than a separate quote call. `dhanhq` is pinned in `requirements.txt` because its price-fetch API surface changes between releases.

### Interactive Brokers (Flex Web Service)
A token + saved Flex query (Open Positions **+** Cash Report in one XML). Multiple logins per entity roll up under one entity via numbered prefixes (`IBKR_DHR_2_*`). The Flex service throttles hard — same-query regeneration returns `1001`, accumulated failures escalate to a `1025` lockout — so `ibkr.py` caches the statement (in-process + on-disk fallback) and `ibkr_holdings_cash_worker.py` makes at most one hit per login. **A fresh token must be regenerated in the IBKR portal once a token hits the stuck state.**

| Variable | Where to find |
|---|---|
| `IBKR_{E}_FLEX_TOKEN` | Client Portal → Settings → Reporting → Flex Web Service |
| `IBKR_{E}_QUERY_ID` | saved Activity Flex Query (Open Positions + Cash Report, XML) |
| `IBKR_{E}_TRADES_QUERY_ID` | optional; Trades-section query for the inception backfill |
| `IBKR_{E}_BASE_CURRENCY` | optional; account base currency for cash (default USD) |
| `IBKR_SYNC_PAUSED` | global kill-switch — `1` keeps the daily sync + cash refresh off IBKR |

### Vested (US) / DBS (SGD) — scraped
Playwright portal scrapers (no public API). `VESTED_{E}_USERNAME` / `_PASSWORD` / `_PIN`; `DBS_{E}_USERNAME` / `_PASSWORD`. Holdings land in `foreign_equity_holding` in native currency, FX-converted to INR.

### Nuvama PMS (WealthSpectrum) — scraped
`NUVAMA_PMS_BASE_URL`, `NUVAMA_PMS_REPORT`, `NUVAMA_PMS_OUTPUT_FORMAT`, and per-entity `NUVAMA_PMS_{E}_USERNAME` / `_PASSWORD` / `_OWNER`.

### Foreign-equity market data (prices, no broker needed)
Independent price feed for `foreign_equity_holding`, tried in order: Finnhub → Twelve Data → yfinance. `FINNHUB_API_KEY` (free tier, US real-time) and optional `TWELVEDATA_API_KEY`; yfinance is the no-key fallback. Finnhub free is US-only, so non-US listings (e.g. an LSE ETF) fall through to yfinance via `symbol_override`.

---

## Gmail OAuth setup (one-time per inbox)

```bash
cd mis-portal/workers
python oauth_setup.py --token gmail_token_central.json
# Opens browser → authorise ***REMOVED*** → token saved
```

One shared token serves both CAS reads and alert sends; re-authorise only as `***REMOVED***`.

---

## AI analyst (Jarvis)

A manual, streaming agentic loop over the Claude Messages API (`assistant/engine.py`). The model emits tool *intent*; the backend executes each client tool against a DB connection already scoped to the resolved entity (`eid`) — **the model is never the security boundary**. Anthropic-hosted server tools (`web_search`, `code_execution`) stream back inline with citations. Conversations persist in `assistant_conversation` / `assistant_message`; model and reasoning effort are chosen per request in `assistant/routing.py`.

---

## Security model

| Control | Implementation |
|---|---|
| Authentication | JWT (2h TTL), httpOnly + Secure + SameSite=Strict cookie |
| Token revocation | Redis blacklist (SHA-256 hash of token); fail-closed |
| Role enforcement | Live DB role check on every request (`_live_role()`), not the JWT claim |
| Entity scoping | Every data tool/route scoped to the caller's entity server-side |
| Password storage | bcrypt, max 72 bytes enforced |
| Brute force | 5-attempt lockout, generic 401 response |
| Rate limiting | `slowapi`, keyed on `CF-Connecting-IP` |
| CSP | Per-request nonce injected by `middleware.ts` |
| Audit | `audit_log` records mutating actions (old/new value) |
| Secrets | `.env` / `.env.local` chmod 600; PAN/email in DB, not env |
| Screenshots | `~/.cams-screenshots/` chmod 700, each file chmod 600 |
| Broker tokens | `equity_tokens.json` chmod 600; atomic write (tmp → rename) |

---

## Systemd services

```bash
sudo systemctl status mis-portal               # FastAPI backend
sudo systemctl status iws-frontend             # Next.js frontend
sudo systemctl status mis-portal-equity-price.timer   # 1-min domestic LTP timer
sudo systemctl restart mis-portal              # restart after config changes
```

`mis-portal-foreign-price.timer` (1-min foreign-equity price refresh, US-hours guarded) is staged in `mis-portal/deploy/systemd/` with install steps in its `README.md`; install it when foreign prices should update live.

Logs: `journalctl -u mis-portal -f`  ·  per-worker logs under `/var/log/mis-portal-*.log`
