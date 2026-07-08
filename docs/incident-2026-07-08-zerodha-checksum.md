# Incident 2026-07-08 — DHR Zerodha "Invalid checksum" (corrupted .env api_secret)

**Status:** Resolved same day.
**Impact:** DHR Zerodha holdings and cash were stale from the 01:00 UTC token refresh until ~04:36 UTC. Both scheduled equity sync runs (7:00 and 10:00 IST) exited 1 with `Errors: ['DHR:zerodha']`.

## Timeline (UTC)

- **01:00** — `equity/token_refresh_worker.py` failed for DHR Zerodha: Kite `generate_session` rejected the exchange with `Invalid checksum`. All other entities (HHR, SDR, Rajani Corp) refreshed fine.
- **01:30** — `equity/equity_sync_worker.py` failed: `[DHR/zerodha] Failed: Incorrect api_key or access_token`. The failure alert email could NOT be sent (see the companion Gmail down-scoping incident).
- **04:30** — the 10:00 IST market-hours catch-up sync failed the same way; this time the alert email went through, which is what surfaced the incident.
- **04:32–04:36** — diagnosed and fixed; manual rerun of equity_sync_worker completed clean (260 holdings, errors: none).

## Root cause

`ZERODHA_DHR_API_SECRET` in `/var/www/mis-portal/.env` was 34 characters instead of Kite's standard 32: the correct secret plus two stray `÷` characters (UTF-8 `c3 b7`) appended at the end — almost certainly an accidental keystroke during the manual .env edit for the 2026-07-07 SDR Zerodha token fix (`Option+/` types `÷` on a Mac keyboard).

Kite's token exchange checksum is SHA-256(api_key + request_token + api_secret). The headless Playwright login itself succeeded (request_token captured, so api_key and login credentials were fine); only the final exchange failed, which is the signature of a wrong api_secret.

## Fix

1. Verified the first 32 characters were the intact real secret by refreshing successfully with the trimmed value.
2. Removed the two `÷` characters from `.env` (asserting 32-char length after the edit).
3. Synced the fresh token to `broker_api_credentials` (the same DB sync `token_refresh_worker.py` normally does).
4. Reran `equity_sync_worker.py` via `cron_wrapper.py` — exit 0, 260 holdings, no errors.

No code change was needed; this was a data/config corruption.

## Lessons / detection rules

- Kite api_secrets are always exactly 32 alphanumeric characters. On any Zerodha `Invalid checksum`, compare that entity's `ZERODHA_*_API_SECRET` length against the other entities before suspecting the broker side.
- `Invalid checksum` with a *successful* headless login means the api_secret is wrong; an expired/consumed request_token produces a different error ("Token is invalid or has expired").
- Workers also rewrite `.env` (Dhan token saves), so manual edits and worker writes share the file — edit carefully and verify the file after manual changes.
- A token-refresh failure at 01:00 UTC deterministically cascades into both daily equity sync runs; the 01:30 failure is the earliest signal.
