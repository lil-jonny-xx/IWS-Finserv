# IWS Finserv MIS Portal

Internal management information system for tracking mutual fund and equity holdings across multiple entities. Built for IWS Finserv staff and analysts.

## What it does

- **Mutual Fund holdings** — automated ingestion from CAMS CAS statements via Gmail, parsed from PDF and stored per entity
- **Equity holdings** — synced live from Angel One, Dhan, and Zerodha via broker APIs
- **Portfolio analytics** — CAGR, realized gains, asset class breakdown, NAV history, FX-adjusted values
- **Multi-entity support** — track holdings across separate PANs/entities with an admin view
- **Dashboard** — consolidated view with column filters, group toggles, and donut charts

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI (Python), PostgreSQL |
| Frontend | Next.js 14, React, TypeScript, Tailwind CSS |
| Auth | JWT + Redis blacklist |
| Automation | Playwright (headless CAMS browser) |
| Email | Gmail API |

## Architecture

```
Next.js frontend  ──►  FastAPI backend  ──►  PostgreSQL
                                │
                    ┌───────────┼───────────────┐
                    ▼           ▼               ▼
              CAS pipeline  Equity sync     NAV / FX
              (CAMS → Gmail  (Angel One,    (AMFI, ECB)
               → PDF parser)  Dhan, Zerodha)
```

**Database schema:** `pan_group`, `entity`, `users`, `holding`, `equity_holding`, `nav_history`, `fx_rate`

## Background workers (cron)

| Worker | Purpose |
|---|---|
| `cams_trigger_worker` | Headless Playwright — submits CAS request on CAMS portal |
| `gmail_worker` | Polls Gmail for CAS PDF attachments |
| `cas_parser_worker` | Extracts holdings from CAS PDF (PyMuPDF) |
| `amfi_nav_worker` | Fetches daily NAVs from AMFI |
| `mf_metrics_worker` | Computes CAGR, XIRR, unrealised P&L per holding |
| `equity_sync_worker` | Syncs holdings from all three brokers |
| `equity_price_worker` | Fetches live equity prices |
| `fx_rates_worker` | Fetches USD/INR and other FX rates |
| `nav_history_backfill` | Backfills historical NAV data |
| `isin_resolver` | Resolves ISINs to scheme metadata |

## Project structure

```
mis-portal/          # FastAPI backend + all workers
  main.py            # API routes, JWT auth, holdings endpoints
  workers/           # Cron workers (one file per job)
  equity/            # Equity module (broker adapters, sync, models)

iws-portal-frontend/ # Next.js frontend
  app/
    dashboard/       # Main dashboard with analytics
    mutual-funds/    # MF holdings table
    equity/          # Equity holdings table
```

## Setup

### Backend

```bash
cd mis-portal
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in DB, broker credentials, Gmail OAuth
python workers/db_migrate_entities.py
python workers/seed_users.py
uvicorn main:app --reload
```

### Frontend

```bash
cd iws-portal-frontend
npm install
cp .env.local.example .env.local   # set NEXT_PUBLIC_API_URL
npm run dev
```

### Gmail OAuth (one-time)

```bash
cd mis-portal
python workers/oauth_setup.py
```

## Environment variables

**`mis-portal/.env`**
```
DATABASE_URL=
JWT_SECRET=
REDIS_URL=

# Angel One
ANGEL_ONE_CLIENT_ID=
ANGEL_ONE_API_KEY=
ANGEL_ONE_TOTP_SECRET=
ANGEL_ONE_PASSWORD=

# Dhan
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=

# Zerodha
ZERODHA_API_KEY=
ZERODHA_API_SECRET=
ZERODHA_REQUEST_TOKEN=
```

**`iws-portal-frontend/.env.local`**
```
NEXT_PUBLIC_API_URL=https://your-backend-url
```

## Security notes

- JWT tokens are blacklisted on logout (Redis-backed)
- CSP nonce injected server-side via `proxy.ts` middleware
- Broker access tokens stored locally, never committed
- Gmail OAuth tokens stored locally, never committed
- CAMS automation uses a persistent browser profile to avoid repeated reCAPTCHA
