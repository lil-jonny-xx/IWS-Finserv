#!/usr/bin/env bash
#
# Phase 4 deploy — sub-second live trades (daemon + SSE endpoint + live Equity UI).
# Run once, as root:   sudo bash /var/www/mis-portal/deploy/live_trades_deploy.sh
#
# Idempotent: re-running just reinstalls units and rebuilds. It does NOT start the
# daemons itself (they auto-start weekdays 09:05 IST via the timer; starting them
# outside market hours is pointless and the token may be stale).
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run with sudo (needs to install systemd units + restart services)"; exit 1; }

REPO=/var/www/mis-portal
UNITS="$REPO/deploy/systemd"
SYSD=/etc/systemd/system

echo "== 1/3  Install live-trade systemd units =="
for u in mis-portal-live-trade@.service \
         mis-portal-live-trade-start.service mis-portal-live-trade-start.timer \
         mis-portal-live-trade-stop.service  mis-portal-live-trade-stop.timer; do
    install -m 0644 "$UNITS/$u" "$SYSD/$u"
    echo "   installed $u"
done
# Service runs as User=SAdmin, so the append log must be writable by it.
touch /var/log/mis-portal-live-trade.log
chown SAdmin:SAdmin /var/log/mis-portal-live-trade.log
systemctl daemon-reload
systemctl enable --now mis-portal-live-trade-start.timer mis-portal-live-trade-stop.timer
echo "   timers:"
systemctl list-timers 'mis-portal-live-trade*' --no-pager || true

echo "== 2/3  Reload backend API (activates GET /api/v1/live/trades) =="
systemctl restart mis-portal.service
sleep 2
systemctl is-active --quiet mis-portal.service && echo "   mis-portal.service active" \
    || { echo "   ERROR: mis-portal.service failed to restart — check: journalctl -u mis-portal -n50"; exit 1; }

echo "== 3/3  Rebuild frontend (live Equity UI) + restart =="
cd /var/www/iws-portal-frontend
sudo -u www-data npm run build
systemctl restart iws-frontend.service
sleep 2
systemctl is-active --quiet iws-frontend.service && echo "   iws-frontend.service active" \
    || { echo "   ERROR: iws-frontend.service failed — check: journalctl -u iws-frontend -n50"; exit 1; }

cat <<'EOF'

== Done ==
The live daemons auto-start weekdays 09:05 IST and stop 15:35 IST.

Start NOW (only meaningful during an open session, and only for a broker you've verified):
  systemctl start mis-portal-live-trade-start.service          # all Zerodha accounts
  systemctl start mis-portal-live-trade@zerodha-hhr.service     # a single account

Watch:
  tail -f /var/log/mis-portal-live-trade.log
  journalctl -u 'mis-portal-live-trade@*' -f

Angel/Dhan: once their order-update WS is verified against a live session, add their
instances (angel-dhr angel-hhr dhan-hhr dhan-rajani) to the start service's list and
re-run this script.
EOF
