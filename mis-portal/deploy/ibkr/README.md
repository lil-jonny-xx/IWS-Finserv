# IBKR real-time streaming — install & bring-up

Real-time positions, per-position P&L, fills, streaming quotes, and market scanners for
every IBKR account we hold — via the TWS API to headless IB Gateways, driven by
`workers/ibkr_stream_daemon.py` (`ib_async` Watchdog + IBC).

This replaces the Flex path for holdings and, critically, sees the accounts Flex was
blind to (`managedAccounts()` returns every account a login can view — the same access as
the portal). Flex stays as the daily authoritative reconcile.

## Topology — FOUR logins = four Gateway sessions

A Gateway session = one IBKR login and sees only that login's accounts, so there is one
daemon instance (and one Gateway) per login:

| Login slug | Entity | Port | Accounts |
|---|---|---|---|
| `sdr`   | SDR | 4001 | master F24091401 + U24221167 / U22101481 / U23958465 / U26030438 (+U24025435) |
| `dhr`   | DHR | 4003 | master U23072488(F) + U23864651 + U24194393  ("3 under 1") |
| `dhr_2` | DHR | 4005 | U23106630  (DHR's 2nd login) |
| `hhr`   | HHR | 4007 | U19356553 |

The account→entity map is baked into `ACCOUNT_ENTITY` in the daemon.

## What is already in the repo
- `workers/ibkr_stream_daemon.py` — the daemon, templated by `--login` (`--smoke` pipe test)
- `deploy/ibkr/config.ini` — IBC template (credentials blank; passed via .env; one copy per login for the port)
- `deploy/systemd/mis-portal-ibkr-stream@.service` — Xvfb-wrapped, persistent, boot-enabled, per-login
- `deploy/tmpfiles/mis-portal-logs.conf` — provisions `/var/log/mis-portal-ibkr-stream.log`
- `ib_async` installed in `/var/www/.venv`

## What YOU install (one time; needs IBKR logins / downloads)

### 1. An API user per login
Each of the four is a separate IBKR login already. For each, in its Client Portal →
**Users & Access Rights**, add a username used ONLY by the daemon (IBKR allows one session
per username; keep it off your interactive logins) and attach the **market-data
subscriptions** you want (US, LSE, AED…) to that user — no subscription = blank quotes.

### 2. IB Gateway (standalone — bundles its own JRE, so no system `java` needed)
Download the **stable standalone** IB Gateway for Linux and install once to
`/opt/ibgateway` (all four instances share the binary). Note the build (e.g. `10.30`) →
`IBKR_GATEWAY_VERSION` as an int, `1030`.

### 3. IBC (IbcAlpha/IBC) + one ini per login
Download the latest from https://github.com/IbcAlpha/IBC to `/opt/ibc`, then make one ini
per login, each with its own `OverrideTwsApiPort`:
```
for L in sdr:4001 dhr:4003 dhr_2:4005 hhr:4007; do
  name=${L%:*}; port=${L#*:}
  sudo cp /var/www/mis-portal/deploy/ibkr/config.ini /opt/ibc/config.$name.ini
  sudo sed -i "s/^OverrideTwsApiPort=.*/OverrideTwsApiPort=$port/" /opt/ibc/config.$name.ini
done
```

### 4. Env (append to /var/www/mis-portal/.env — no secrets in git)
```
# shared
IBKR_GATEWAY_VERSION=1030
IBKR_GATEWAY_PATH=/opt/ibgateway
IBKR_IBC_PATH=/opt/ibc
IBKR_TRADING_MODE=live
IBKR_READONLY=true
# per login (repeat for SDR / DHR / DHR_2 / HHR):
IBKR_SDR_TWS_USERID=<api-user>
IBKR_SDR_TWS_PASSWORD=<password>
IBKR_SDR_API_PORT=4001
IBKR_SDR_API_CLIENT_ID=17
# IBKR_SDR_IBC_INI defaults to /opt/ibc/config.sdr.ini
# IBKR_SDR_TWS_SETTINGS_PATH defaults to /opt/ibgateway/settings_sdr
IBKR_DHR_TWS_USERID=... ; IBKR_DHR_TWS_PASSWORD=... ; IBKR_DHR_API_PORT=4003
IBKR_DHR_2_TWS_USERID=... ; IBKR_DHR_2_TWS_PASSWORD=... ; IBKR_DHR_2_API_PORT=4005
IBKR_HHR_TWS_USERID=... ; IBKR_HHR_TWS_PASSWORD=... ; IBKR_HHR_API_PORT=4007
```

### 5. One-time Gateway API settings (per login)
`AcceptIncomingConnectionAction=accept` (in each ini) auto-clears the connection prompt,
so a Trusted IP isn't strictly required. To capture executions placed from *other*
sessions (mobile/TWS), set each Gateway's **Master API client ID = 17** once (Configure →
API → Settings) via a one-off VNC/X11 session, or by editing the saved settings under that
login's `settings_<slug>` dir.

## Bring-up

```bash
# provision the log file (idempotent; safe to re-run)
sudo install -m0644 /var/www/mis-portal/deploy/tmpfiles/mis-portal-logs.conf /etc/tmpfiles.d/mis-portal-logs.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/mis-portal-logs.conf

# 1) SMOKE TEST one login first — proves login + account enumeration + positions, exits
cd /var/www/mis-portal
xvfb-run -a /var/www/.venv/bin/python -m workers.ibkr_stream_daemon --login SDR --smoke
#   expect: "[SDR] managed accounts: [...]" and "[SDR] SMOKE OK — accounts=[...] positions=N"
# repeat for DHR / DHR_2 / HHR

# 2) Install the template unit and enable one instance per login
sudo install -m0644 /var/www/mis-portal/deploy/systemd/mis-portal-ibkr-stream@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mis-portal-ibkr-stream@sdr mis-portal-ibkr-stream@dhr \
                            mis-portal-ibkr-stream@dhr_2 mis-portal-ibkr-stream@hhr
tail -f /var/log/mis-portal-ibkr-stream.log
```

Flags: `--quotes` also streams top-of-book on held names (counts against the ~100
market-data-line quota); `--scanner` runs a starter US top-gainers scan. Both are off by
default; add them to the unit's `ExecStart` once the smoke test is green.

## Current status of the daemon
Connectivity + streaming **scaffold**: every stream is subscribed and its events are
LOGGED. The DB/SSE integration — position/P&L → `foreign_equity_holding`, fills →
`stock_transaction` (`source_ref=ibkr:live:{execId}`) + the `live_trades` SSE channel,
quote fan-out — is the **next milestone** (marked `TODO(next)` in the handlers). Get a
smoke test green and the log showing live POSITION/FILL/QUOTE lines, then I wire the
handlers into the DB and the existing live-trade UI, with the Flex **Trades query** as the
Tier-2 reconcile that supersedes `ibkr:live:` rows.
