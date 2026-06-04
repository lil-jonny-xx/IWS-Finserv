# IWS Finserv MIS Portal

Internal portfolio management and analytics system for IWS Finserv and its 6 linked investment entities. Automates the full data pipeline — from CAMS CAS statement ingestion through broker API sync — and presents consolidated holdings, P&L, and analytics through a Next.js dashboard.

**Entities:** DHR (***REMOVED***), HHR (***REMOVED***), ADR (***REMOVED***), SDR (***REMOVED***), IWS (IWS Finserv), IWSFC (IWS Fincorp)

---

## What it does

- **Mutual Fund holdings** — nightly automated ingestion from CAMS CAS PDFs via central Gmail inbox; parsed per entity using folio holder-name matching
- **Equity holdings** — daily sync from Angel One (SmartAPI), Zerodha (Kite Connect), and Dhan; live LTP updates every minute during market hours
- **Portfolio analytics** — CAGR, realized/unrealized P&L, YTD P&L, asset class breakdown, weekly delta, NAV history, FX-adjusted values
- **Multi-entity support** — admin view across all PANs/entities; per-entity view for non-admin users
- **Overview dashboard** — consolidated asset allocation donut, P&L summary, fund breakdown bars, analytics tables
- **Security** — JWT auth, Redis-backed token blacklist, bcrypt passwords, CSP nonce, rate limiting, account lockout

---

## Stack

| Layer | Tech |
|---|---|
| Backend | FastAPI (Python 3.12), Gunicorn + Uvicorn workers |
| Frontend | Next.js 14, React, TypeScript, Tailwind CSS |
| Database | PostgreSQL 16 |
| Auth | JWT (2h TTL) + Redis blacklist + httpOnly cookies |
| Automation | Playwright (Chromium), playwright-stealth, Xvfb |
| Email | Gmail API (OAuth 2.0) |
| Reverse proxy | Nginx + Cloudflare |

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
Nginx :443  (SSL via Cloudflare origin cert)
  │         X-Frame-Options, HSTS, CSP, Referrer-Policy
  │
  ├── /        ──► Next.js :3000  (iws-frontend.service)
  │                middleware.ts injects per-request CSP nonce
  │                Pages: / (login)  /dashboard  /equity  /mutual-funds
  │
  └── /api/    ──► Gunicorn + Uvicorn :8000  (mis-portal.service, 3 workers)
                   FastAPI app — mis-portal/main.py
```

### Data pipelines

```
NIGHTLY CAS PIPELINE  (11 PM IST → up to 8 AM IST)
  cas_automation_worker.py  (orchestrator, SAdmin cron)
    ├── cams_trigger_worker.py   Playwright fills CAMS form per entity
    ├── gmail_worker.py          Gmail API polls ***REMOVED*** every 30s
    └── cas_parser_worker.py     PyMuPDF + casparser → upsert DB

EQUITY PIPELINE  (daily)
  token_refresh_worker.py   (6:30 AM) — refreshes broker OAuth tokens
  equity_sync_worker.py     (7:00 AM) — syncs holdings from all 3 brokers
  equity_price_worker.py    (every 1 min, 9:15–15:30 IST) — live LTP

ENRICHMENT WORKERS  (daily)
  amfi_nav_worker.py        (10:00 PM) — AMFI NAV CSV → nav_history
  mf_metrics_worker.py      (10:15 PM) — computes CAGR, P&L, exposure%
  fx_rates_worker.py        (12:30 AM) — FX rates → fx_rate table
```

---

## Project structure

```
/var/www/
├── mis-portal/
│   ├── main.py                  FastAPI app — all API routes + auth
│   ├── .env                     Secrets (600, not committed)
│   ├── equity/
│   │   ├── equity_sync_worker.py
│   │   ├── equity_price_worker.py
│   │   ├── token_refresh_worker.py
│   │   ├── tokens.py            Broker token store (atomic write)
│   │   └── brokers/
│   │       ├── zerodha.py       Kite Connect adapter
│   │       ├── angel_one.py     SmartAPI adapter
│   │       └── dhan.py          Dhan API adapter
│   └── workers/
│       ├── cas_automation_worker.py
│       ├── cams_trigger_worker.py
│       ├── gmail_worker.py
│       ├── cas_parser_worker.py
│       ├── amfi_nav_worker.py
│       ├── mf_metrics_worker.py
│       ├── fx_rates_worker.py
│       └── nav_history_backfill.py
│
├── iws-portal-frontend/
│   ├── app/
│   │   ├── page.tsx             Login page
│   │   ├── dashboard/           Portfolio overview + analytics
│   │   ├── equity/              Equity holdings table (sector-grouped)
│   │   └── mutual-funds/        MF holdings table (CAGR, realized gain, filters)
│   ├── middleware.ts             CSP nonce injection
│   └── .env.local               API URL (600, not committed)
│
└── docs/
    ├── recap-2026-06-02.md      Full architecture recap (May 18 – Jun 2)
    └── work-log-YYYY-MM-DD.md   Daily work logs
```

---

## Database schema

```
pan_group          entity                  users
─────────          ──────                  ─────
id PK              id PK                   id PK
pan_name UNIQUE    entity_name UNIQUE       email UNIQUE
pan_number         pan_group_id FK          password_hash
                   cas_email               role (admin/user)
                                           entity_id FK
                                           is_active, failed_attempts
                                           locked_until

security_master    folio_mapping           holding  (MF)
───────────────    ─────────────           ──────────────
id PK              folio_number PK         id PK
isin UNIQUE        entity_id FK            entity_id FK
security_name      mf_scheme               security_id FK
security_type                              folio_number
asset_class                                quantity, cost_basis, avg_cost
amfi_code                                  first_invested_date
                                           last_updated_nav
                                           market_value_as_on
                                           pnl_inception, pnl_ytd
                                           cagr_inception_pct
                                           exposure_pct, weekly_change

equity_holding     nav_history             mf_transaction
──────────────     ───────────             ──────────────
id PK              id PK                   id PK
entity_id FK       security_id FK          entity_id FK
broker             nav_date, nav           security_id FK
symbol, isin       UNIQUE(sec, date)       folio_number
sector                                     transaction_date
quantity, avg_cost                         amount, units, nav
current_price                              transaction_type
market_value
pnl, exposure_pct
prev_week_value
pnl_ytd, weekly_change
UNIQUE(entity, broker, symbol)
```

---

## Cron schedule (all times IST, SAdmin crontab)

| Time | Worker | Log |
|---|---|---|
| 12:30 AM | `fx_rates_worker.py` | `/var/log/mis-portal-fx-worker.log` |
| 6:30 AM | `token_refresh_worker.py` | — |
| 7:00 AM | `equity_sync_worker.py` | — |
| Every 1 min (09:15–15:30) | `equity_price_worker.py` | `/home/SAdmin/mis-portal-equity-price.log` |
| 10:00 PM | `amfi_nav_worker.py` | `/var/log/mis-portal-amfi-worker.log` |
| 10:15 PM | `mf_metrics_worker.py` | `/var/log/mis-portal-mf-metrics.log` |
| 11:00 PM | `cas_automation_worker.py` | `/var/log/mis-portal-cas-auto.log` |

---

## Broker API credentials

Each broker requires per-entity credentials in `mis-portal/.env`. PAN numbers and CAS emails are stored in the database (`pan_group.pan_number`, `entity.cas_email`) — not in `.env`.

### Angel One (SmartAPI)
| Variable | Where to find |
|---|---|
| `ANGEL_{E}_API_KEY` | My Profile → Apps in the Angel One web portal |
| `ANGEL_{E}_CLIENT_ID` | Your Angel One login username (e.g. `***REMOVED***`) |
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
| Variable | Where to find |
|---|---|
| `DHAN_{E}_CLIENT_ID` | Dhan account ID |
| `DHAN_{E}_ACCESS_TOKEN` | Generated from Dhan developer portal (valid 30 days — refresh monthly) |

---

## Gmail OAuth setup (one-time per inbox)

```bash
cd mis-portal/workers
python oauth_setup.py --token gmail_token_central.json
# Opens browser → authorise ***REMOVED*** → token saved
```

---

## Security model

| Control | Implementation |
|---|---|
| Authentication | JWT (2h TTL), httpOnly + Secure + SameSite=Strict cookie |
| Token revocation | Redis blacklist (SHA-256 hash of token); fail-closed |
| Role enforcement | Live DB role check on every request (`_live_role()`), not from JWT claim |
| Password storage | bcrypt, max 72 bytes enforced |
| Brute force | 5-attempt lockout, generic 401 response |
| Rate limiting | `slowapi`, keyed on `CF-Connecting-IP` |
| CSP | Per-request nonce injected by `middleware.ts` |
| Secrets | `.env` and `.env.local` chmod 600; PAN/email in DB not env |
| Screenshots | `~/.cams-screenshots/` chmod 700, each file chmod 600 |
| Broker tokens | `equity_tokens.json` chmod 600; atomic write (tmp → rename) |

---

## Systemd services

```bash
sudo systemctl status mis-portal        # FastAPI backend (mis-portal-svc user)
sudo systemctl status iws-frontend      # Next.js frontend
sudo systemctl restart mis-portal       # restart after config changes
```

Logs: `journalctl -u mis-portal -f`
