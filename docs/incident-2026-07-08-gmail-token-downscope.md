# Incident 2026-07-08 — Cron failure alerts 403 (gmail_worker down-scoped the shared Gmail token)

**Status:** Resolved same day (commit `2fc566f`).
**Impact:** Cron failure alert emails intermittently failed with `403 insufficientPermissions` ("Request had insufficient authentication scopes") for weeks — reliably in the 01:00–01:30 UTC window, which is exactly when the token refresh and equity sync workers report failures. Real failures (e.g. the DHR Zerodha checksum incident the same day, and earlier the HHR Dhan TOTP failure on 07-07) went unalerted.

## The tell

Alerts failed at 01:01 and 01:30 UTC but succeeded at 04:30 UTC on both 07-07 and 07-08 — the same code, same token file, different hours. That pattern means the token file's *contents* were changing during the day.

## Root cause

One shared Gmail OAuth token — `mis-portal/workers/gmail_token_central.json` (account ***REMOVED***) — is used by both:

- **Readers:** `workers/gmail_worker.py` `_get_service()`, used by the CAS collector (`cas_automation_worker.py`), the ICICI PMS OTP reader (`icici_pms_worker.py`), and `manual_cas_retrigger.py`.
- **Sender:** `workers/alert.py`, used by `cron_wrapper.py` to email cron failures.

`gmail_worker.py` declared `SCOPES = [gmail.readonly]` only. google-auth's `Credentials.refresh()` sends its scope list in the refresh grant (`reauth.refresh_grant(..., scopes=self._scopes)`), so Google issued a **down-scoped access token** carrying only `gmail.readonly` — and `_get_service()` wrote that token back to the shared file.

Sequence each night:

1. The CAS collector / OTP poller runs overnight and refreshes the token → shared file now holds a readonly-only access token.
2. At 01:00–01:30 UTC, `cron_wrapper.py` tries to send a failure alert. `alert.py` loads the file; the access token is still valid (not expired) so no refresh happens, and the send gets `403 insufficientPermissions`.
3. ~1 hour later the access token expires. The next alert attempt makes `alert.py` refresh with its own full scope list (readonly + send) → full-scope token restored → alerts work again (e.g. at 04:30 UTC).

The underlying refresh-token grant always had both scopes (granted via `oauth_setup.py`); only the short-lived access token was being narrowed. No re-auth was required.

## Fix

`workers/gmail_worker.py` now requests the full granted scope set — `gmail.readonly` + `gmail.send` — matching `oauth_setup.py` and `alert.py`, with a comment explaining why it must never be narrowed. Committed and pushed as `2fc566f`.

All consumers of `gmail_worker` are cron-launched (fresh process per run), so no service restarts were needed.

## Verification

Forced the token file to expired, refreshed through `gmail_worker._get_service()` (the previously-breaking path), confirmed the rewritten file kept both scopes, then confirmed inbox read AND a live alert send both succeeded on the freshly-written token.

## Lessons

- **Every loader of a shared OAuth token must request the full granted scope set.** A single narrower loader silently down-scopes the shared access token for everyone on each refresh.
- Time-of-day-dependent auth failures on a shared credential file point to another writer changing the file, not to the grant itself.
- This coupling is a known trap on this token (re-auth account choice; see the shared-token notes) — the scope list is a second dimension of the same trap.
