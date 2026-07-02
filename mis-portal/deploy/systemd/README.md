# Foreign Equity Price Worker — systemd install

Refreshes `foreign_equity_holding` native prices every minute during US market hours
(09:30–16:00 ET), via Finnhub → Twelve Data → yfinance. Mirrors the existing
`mis-portal-equity-price` timer. Requires `FINNHUB_API_KEY` (and optionally
`TWELVEDATA_API_KEY`) in `/var/www/mis-portal/.env`; yfinance is the no-key fallback.

## Install (needs sudo)

```bash
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-foreign-price.service /etc/systemd/system/
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-foreign-price.timer   /etc/systemd/system/
sudo touch /var/log/mis-portal-foreign-price.log
sudo chown SAdmin:SAdmin /var/log/mis-portal-foreign-price.log
sudo systemctl daemon-reload
sudo systemctl enable --now mis-portal-foreign-price.timer
```

## Verify

```bash
systemctl list-timers mis-portal-foreign-price.timer
systemctl status mis-portal-foreign-price.service
tail -f /var/log/mis-portal-foreign-price.log
```

Outside US hours the log just shows `US market closed — skipping`. To force a one-off
run any time: `/var/www/.venv/bin/python /var/www/mis-portal/workers/foreign_price_worker.py --commit --force`

## Rate-limit note
Finnhub free = 60 calls/min; ~45 symbols = 45 calls/run, so the 1-minute cadence is the
most real-time refresh that stays under the quota. If you add many more foreign holdings
(>60), widen `OnUnitActiveSec` in the timer (e.g. 120) or add a Twelve Data key so load
spreads across providers.

---

# Equity Snapshot Worker — systemd install

Snapshots every entity's live Indian-broker equity positions at market open, hourly, and
close, then diffs consecutive ticks to detect the day's buys/sells. Detected trades are
written to `stock_transaction` (`source='snapshot'`) so they feed **Realised Gains** and the
Equity page's **"Traded today"** panel; on an account's first snapshot it seeds an opening
BUY per holding (`source='snapshot_open'`) from the broker avg cost so later sells net to a
correct realised P&L. A later real tradebook import supersedes the synthetic rows.

Run the one-time DB migration first:

```bash
/var/www/.venv/bin/python -m workers.db_migrate_equity_snapshots   # from /var/www/mis-portal
```

## Install (needs sudo)

```bash
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-equity-snapshot.service /etc/systemd/system/
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-equity-snapshot.timer   /etc/systemd/system/
sudo touch /var/log/mis-portal-equity-snapshot.log
sudo chown SAdmin:SAdmin /var/log/mis-portal-equity-snapshot.log
sudo systemctl daemon-reload
sudo systemctl enable --now mis-portal-equity-snapshot.timer
```

## Verify

```bash
systemctl list-timers mis-portal-equity-snapshot.timer
tail -f /var/log/mis-portal-equity-snapshot.log
```

Force a one-off snapshot any time during market hours:
`/var/www/.venv/bin/python /var/www/mis-portal/workers/equity_snapshot_worker.py`

## Cadence note
Ticks are UTC (server clock): open `03:45`, hourly `04:45–09:45`, close `10:00`
(= 09:15 / 10:15–15:15 / 15:30 IST). The worker self-guards `09:00–15:45 IST` and exits
instantly off-session, so it never runs outside market hours even if the timer fires.

---

# Foreign trade capture — Vested snapshot + IBKR Flex (systemd install)

Foreign equity uses `equity_trade_ledger` (native currency + trade-date FX), NOT
`stock_transaction`. Two workers keep the day's foreign buys/sells flowing into Foreign
Realised Gains and the Foreign Equity **"Traded today"** panel:

- **Vested** — scraped (no trades API), so `foreign_snapshot_worker.py` snapshots positions
  every ~2h and diffs them (quantities from the scrape, price/avg from the synced holding),
  writing detected trades as `source='snapshot'`.
- **IBKR** — `ibkr_holdings_cash_worker.py` pulls open positions + cash + 365d of **exact
  executed trades** in ONE Flex statement (`source='ibkr_flex'`, deduped by tradeID). No
  diffing. Run pre-market and post-market (US Eastern).

## Install (needs sudo)

```bash
# Vested snapshot (every 2h, weekdays)
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-foreign-snapshot.service /etc/systemd/system/
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-foreign-snapshot.timer   /etc/systemd/system/
sudo touch /var/log/mis-portal-foreign-snapshot.log
sudo chown SAdmin:SAdmin /var/log/mis-portal-foreign-snapshot.log

# IBKR Flex (pre-market + post-market US)
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-ibkr-flex.service /etc/systemd/system/
sudo cp /var/www/mis-portal/deploy/systemd/mis-portal-ibkr-flex.timer   /etc/systemd/system/
# /var/log/mis-portal-ibkr.log already exists from the old cron

sudo systemctl daemon-reload
sudo systemctl enable --now mis-portal-foreign-snapshot.timer
sudo systemctl enable --now mis-portal-ibkr-flex.timer
```

**Remove the old once-daily IBKR cron** so the token isn't hit a third time — delete this
line from the crontab (`crontab -e`):

```
30 2 * * * IBKR_FLEX_SEND_RETRIES=1 /var/www/.venv/bin/python /var/www/mis-portal/workers/cron_wrapper.py workers/ibkr_holdings_cash_worker.py --commit >> /var/log/mis-portal-ibkr.log 2>&1
```

## Verify

```bash
systemctl list-timers 'mis-portal-foreign-snapshot.timer' 'mis-portal-ibkr-flex.timer'
tail -f /var/log/mis-portal-foreign-snapshot.log /var/log/mis-portal-ibkr.log
```

Force a one-off run:
`/var/www/.venv/bin/python /var/www/mis-portal/workers/foreign_snapshot_worker.py`
`/var/www/.venv/bin/python /var/www/mis-portal/workers/ibkr_holdings_cash_worker.py --commit`

## Notes
- IBKR schedule is US-Eastern (`America/New_York` in the timer → DST-safe): 09:00 ET pre-market
  (13:00 UTC summer) + 16:45 ET post-market (20:45 UTC summer). One Flex call/run, well within
  IBKR's 1/sec + 10/min limits.
- The IBKR worker runs even while `IBKR_SYNC_PAUSED` is set (it is the deliberate paced path),
  so it stays ready — but succeeds only once the token lockout clears.
- Vested detected trades are native USD; realised P&L (INR) is computed by the Foreign realised
  engine at each leg's trade-date FX.
