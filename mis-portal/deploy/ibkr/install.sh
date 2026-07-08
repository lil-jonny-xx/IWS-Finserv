#!/usr/bin/env bash
# One-shot installer for the IBKR real-time streaming stack (server side only).
#
#   sudo bash /var/www/mis-portal/deploy/ibkr/install.sh
#
# Idempotent: safe to re-run. Does everything that DOESN'T need IBKR credentials:
#   1. IB Gateway (standalone, bundles its own JRE)  -> /opt/ibgateway
#   2. IBC (IbcAlpha/IBC)                            -> /opt/ibc  + 4 per-login inis
#   3. /var/log/mis-portal-ibkr-stream.log           (via tmpfiles)
#   4. systemd template unit mis-portal-ibkr-stream@ (installed + daemon-reload, NOT enabled)
#
# It deliberately does NOT: write .env, enable/start services, or smoke-test —
# those come after you fill the per-login credentials in .env. See README.md.
set -euo pipefail

REPO=/var/www/mis-portal
GW_DIR=/opt/ibgateway
IBC_DIR=/opt/ibc
IBC_VERSION="${IBC_VERSION:-3.24.1}"
GW_URL=https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
IBC_URL="https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip"
SVC_USER="${SVC_USER:-SAdmin}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

[ "$(id -u)" -eq 0 ] || { echo "run with sudo/root" >&2; exit 1; }
echo "==> staging in $STAGE"

# --- 1. IB Gateway ---------------------------------------------------------
if [ -x "$GW_DIR/ibgateway" ] || [ -d "$GW_DIR" ] && ls "$GW_DIR" >/dev/null 2>&1 && [ -n "$(ls -A "$GW_DIR" 2>/dev/null)" ]; then
  echo "==> IB Gateway already present at $GW_DIR (skipping download)"
else
  echo "==> downloading IB Gateway (~320MB)"
  curl -fL --retry 3 -o "$STAGE/ibgw.sh" "$GW_URL"
  echo "==> installing IB Gateway -> $GW_DIR"
  sh "$STAGE/ibgw.sh" -q -dir "$GW_DIR"
fi

# --- 2. IBC ----------------------------------------------------------------
if [ -f "$IBC_DIR/gatewaystart.sh" ] || [ -f "$IBC_DIR/scripts/ibcstart.sh" ]; then
  echo "==> IBC already present at $IBC_DIR (skipping download)"
else
  echo "==> downloading IBC ${IBC_VERSION}"
  curl -fL --retry 3 -o "$STAGE/ibc.zip" "$IBC_URL"
  mkdir -p "$IBC_DIR"
  echo "==> unzipping IBC -> $IBC_DIR"
  if command -v unzip >/dev/null 2>&1; then
    unzip -o "$STAGE/ibc.zip" -d "$IBC_DIR" >/dev/null
  else
    # unzip not installed — use the venv's Python stdlib zip extractor
    /var/www/.venv/bin/python -m zipfile -e "$STAGE/ibc.zip" "$IBC_DIR"
  fi
  chmod +x "$IBC_DIR"/*.sh "$IBC_DIR"/scripts/*.sh 2>/dev/null || true
fi

# --- 2b. one ini per login (only the API port differs) ---------------------
echo "==> generating per-login IBC inis from repo template"
for L in sdr:4001 dhr:4003 dhr_2:4005 hhr:4007; do
  name="${L%:*}"; port="${L#*:}"; dst="$IBC_DIR/config.$name.ini"
  cp "$REPO/deploy/ibkr/config.ini" "$dst"
  sed -i "s/^OverrideTwsApiPort=.*/OverrideTwsApiPort=$port/" "$dst"
  echo "    $dst  (port $port)"
done

# --- 3. log file via tmpfiles ---------------------------------------------
echo "==> installing tmpfiles + creating log file"
install -m0644 "$REPO/deploy/tmpfiles/mis-portal-logs.conf" /etc/tmpfiles.d/mis-portal-logs.conf
systemd-tmpfiles --create /etc/tmpfiles.d/mis-portal-logs.conf

# --- 4. systemd template unit (installed, NOT enabled) --------------------
echo "==> installing systemd template unit"
install -m0644 "$REPO/deploy/systemd/mis-portal-ibkr-stream@.service" /etc/systemd/system/
systemctl daemon-reload

# --- detect Gateway build for IBKR_GATEWAY_VERSION -------------------------
echo
echo "==> DONE. Server-side install complete."
GW_VER="$(grep -hoE '10[0-9]{2}' "$GW_DIR"/.install4j/i4jparams.conf 2>/dev/null | head -1 || true)"
[ -z "$GW_VER" ] && GW_VER="$(ls -d "$GW_DIR"/*/ 2>/dev/null | grep -oE '10[0-9]{2}' | head -1 || true)"
echo
echo "NEXT (needs your IBKR credentials — do these last):"
if [ -n "$GW_VER" ]; then
  echo "  • .env: IBKR_GATEWAY_VERSION=$GW_VER   (detected build)"
else
  echo "  • .env: set IBKR_GATEWAY_VERSION to the major build — inspect: ls $GW_DIR"
fi
echo "  • .env: fill IBKR_{SDR,DHR,DHR_2,HHR}_TWS_USERID / _TWS_PASSWORD"
echo "  • smoke-test:  cd $REPO && xvfb-run -a /var/www/.venv/bin/python -m workers.ibkr_stream_daemon --login SDR --smoke"
echo "  • enable:      systemctl enable --now mis-portal-ibkr-stream@sdr @dhr @dhr_2 @hhr"
