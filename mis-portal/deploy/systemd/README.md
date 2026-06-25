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
