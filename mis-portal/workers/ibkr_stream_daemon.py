"""
IBKR real-time streaming daemon (TWS API via IB Gateway, driven by ib_async).

ONE persistent connection PER IBKR LOGIN gives us, across every subaccount that login
can see, all four real-time streams the Indian-broker live daemon can't provide for IBKR:

  (1) LIVE BOOK      positions + per-position/per-account P&L  (positionEvent / pnl*Event)
  (2) REAL-TIME FILLS executions as they happen               (execDetailsEvent)
  (3) STREAMING QUOTES top-of-book on held names / watchlist   (pendingTickersEvent)
  (4) MARKET SCANNER  live scanner rows                        (scannerDataEvent)

There are FOUR IBKR logins (one Flex token each in .env), so this daemon is templated
by --login and runs as one instance per login (like the Indian-broker @.service):

  LOGIN    ENTITY  ACCOUNTS (a Gateway session = one login, sees only its own accounts)
  SDR      SDR     advisor master F24091401 + U242.. / U221.. / U239.. / U260.. / U240..
  DHR      DHR     master U23072488(F) + U23864651 + U24194393   (the "3 under 1")
  DHR_2    DHR     U23106630                                     (DHR's 2nd login)
  HHR      HHR     U19356553

The Gateway is a GUI Java app; ib_async's Watchdog+IBC launch and babysit it (auto-relogin
on IBKR's forced daily restart, auto-reconnect on drop). Each instance runs under its own
Xvfb display, its own API port, and its own IBC ini. See deploy/ibkr/README.md.

STATUS: connectivity + streaming scaffold. Event handlers currently LOG only. The DB/SSE
integration (foreign_equity_holding upserts, fills -> stock_transaction +
'live_trades' channel with source_ref 'ibkr:live:{execId}', quote fan-out) is the next
milestone and is marked TODO at each handler. Run `--login X --smoke` first to prove the
pipe once Gateway+IBC are installed.

Env (loaded from /var/www/mis-portal/.env):
  # per-login (X in {SDR,DHR,DHR_2,HHR}) — an API-only Client Portal user per login:
  IBKR_{X}_TWS_USERID, IBKR_{X}_TWS_PASSWORD
  IBKR_{X}_API_PORT                     distinct per login (e.g. 4001/4003/4005/4007)
  IBKR_{X}_API_CLIENT_ID  (default 17)  set the Gateway 'Master API client ID' to this
  IBKR_{X}_IBC_INI        (default /opt/ibc/config.{x}.ini)   its own ini (own port)
  IBKR_{X}_TWS_SETTINGS_PATH (default {GATEWAY_PATH}/settings_{x})  own settings dir
  # shared:
  IBKR_GATEWAY_VERSION                  e.g. 1030 (the installed IB Gateway build)
  IBKR_GATEWAY_PATH                     install dir of IB Gateway (twsPath)
  IBKR_IBC_PATH                         IBC install dir
  IBKR_TRADING_MODE  (default live)
  IBKR_READONLY      (default true)     monitoring only; set false only to place orders
"""
import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv("/var/www/mis-portal/.env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ibkr_stream")

# --- logins ----------------------------------------------------------------
# Default API ports, spaced by 2 to leave paper/next room. Overridable per login.
LOGINS = {
    "SDR":   {"default_port": 4001},
    "DHR":   {"default_port": 4003},
    "DHR_2": {"default_port": 4005},
    "HHR":   {"default_port": 4007},
}

# --- account -> entity map (ground truth from the Flex statements per login) --
# Every account a login can see maps to its entity. Unknown accounts are logged and
# skipped (never silently dropped). Entity ids: DHR=7, HHR=10, SDR=12.
ACCOUNT_ENTITY = {
    # SDR login
    "U24221167": (12, "SDR"), "U22101481": (12, "SDR"), "U23958465": (12, "SDR"),
    "U26030438": (12, "SDR"), "U24025435": (12, "SDR"), "F24091401": (12, "SDR"),
    # DHR login (master U23072488 + 3)
    "U23072488": (7, "DHR"), "U23072488F": (7, "DHR"),
    "U23864651": (7, "DHR"), "U24194393": (7, "DHR"),
    # DHR_2 login
    "U23106630": (7, "DHR"),
    # HHR login
    "U19356553": (10, "HHR"),
}


def _env(name, default=None, required=False):
    v = os.getenv(name, default)
    if required and not v:
        log.error("missing required env var %s", name)
        sys.exit(2)
    return v


def _login_env(login, suffix, default=None, required=False):
    """Per-login env var IBKR_{LOGIN}_{SUFFIX}."""
    return _env(f"IBKR_{login}_{suffix}", default=default, required=required)


def build_ibc(login):
    from ib_async import IBC
    low = login.lower()
    settings = _login_env(login, "TWS_SETTINGS_PATH",
                          f"{_env('IBKR_GATEWAY_PATH', required=True)}/settings_{low}")
    ibc_ini = _login_env(login, "IBC_INI", f"/opt/ibc/config.{low}.ini")
    return IBC(
        int(_env("IBKR_GATEWAY_VERSION", required=True)),
        gateway=True,
        tradingMode=_env("IBKR_TRADING_MODE", "live"),
        twsPath=_env("IBKR_GATEWAY_PATH", required=True),
        twsSettingsPath=settings,
        ibcPath=_env("IBKR_IBC_PATH", required=True),
        ibcIni=ibc_ini,
        userid=_login_env(login, "TWS_USERID", required=True),
        password=_login_env(login, "TWS_PASSWORD", required=True),
    )


# --- event handlers (LOG-only scaffold; DB/SSE wiring is the next milestone) --
def on_position(pos):
    ent = ACCOUNT_ENTITY.get(pos.account)
    if ent is None:
        log.warning("position for unmapped account %s (%s) — skipping",
                    pos.account, getattr(pos.contract, "symbol", "?"))
        return
    log.info("POSITION [%s] %s %s qty=%s avgCost=%s", ent[1], pos.account,
             pos.contract.localSymbol or pos.contract.symbol, pos.position, pos.avgCost)
    # TODO(next): upsert foreign_equity_holding(entity, contract, qty, avgCost) + SSE


def on_pnl(pnl):
    log.info("PNL acct=%s daily=%s unreal=%s real=%s",
             pnl.account, pnl.dailyPnL, pnl.unrealizedPnL, pnl.realizedPnL)
    # TODO(next): account-level P&L into UI


def on_pnl_single(p):
    log.info("PNL1 acct=%s conId=%s pos=%s daily=%s unreal=%s value=%s",
             p.account, p.conId, p.position, p.dailyPnL, p.unrealizedPnL, p.value)
    # TODO(next): per-position live P&L into foreign_equity_holding row


def on_exec(trade, fill):
    ex = fill.execution
    log.info("FILL acct=%s %s %s %s @ %s execId=%s",
             ex.acctNumber, ex.side, ex.shares,
             trade.contract.localSymbol or trade.contract.symbol, ex.price, ex.execId)
    # TODO(next): write stock_transaction source='ibkr'
    #   source_ref=f"ibkr:live:{ex.execId}"  -> Tier-1; Flex Trades query (Tier-2)
    #   supersedes via ibkr:{tradeId}. Publish to LIVE_TRADES_CHANNEL for the SSE UI.


def on_ticks(tickers):
    for t in tickers:
        log.info("QUOTE %s last=%s bid=%s ask=%s",
                 t.contract.localSymbol or t.contract.symbol, t.last, t.bid, t.ask)
    # TODO(next): fan out to Redis + SSE; demote the 60s foreign_price_worker to fallback


def on_scan(data):
    log.info("SCAN %d rows", len(data))
    for row in data[:10]:
        log.info("  #%s %s", row.rank, row.contractDetails.contract.symbol)
    # TODO(next): surface scanner hits to the screening UI


def wire(ib):
    ib.positionEvent += on_position
    ib.pnlEvent += on_pnl
    ib.pnlSingleEvent += on_pnl_single
    ib.execDetailsEvent += on_exec
    ib.pendingTickersEvent += on_ticks
    ib.scannerDataEvent += on_scan


def subscribe(ib, quotes=False, scanner=False):
    """Open the streaming subscriptions once connected."""
    accts = ib.managedAccounts()
    log.info("managed accounts: %s", accts)
    unmapped = [a for a in accts if a not in ACCOUNT_ENTITY]
    if unmapped:
        log.warning("accounts not in ACCOUNT_ENTITY map: %s", unmapped)

    # (1) LIVE BOOK — <50 subaccounts per login so plain reqPositions streams them all
    positions = ib.reqPositions()
    log.info("initial positions: %d rows", len(positions))
    for acct in accts:
        ib.reqPnL(acct)                          # account-level realtime P&L
    for p in positions:                          # per-position P&L needs (acct, conId)
        if p.position:
            ib.reqPnLSingle(p.account, "", p.contract.conId)

    # (3) STREAMING QUOTES — off by default (each line counts against the ~100
    #     market-data-line quota and needs an exchange subscription on the user)
    if quotes:
        for p in positions:
            if p.position:
                ib.reqMktData(p.contract, "", False, False)

    # (4) MARKET SCANNER — off by default; a starter US top-gainers scan
    if scanner:
        from ib_async import ScannerSubscription
        sub = ScannerSubscription(instrument="STK",
                                  locationCode="STK.US.MAJOR",
                                  scanCode="TOP_PERC_GAIN",
                                  numberOfRows=50)
        ib.reqScannerSubscription(sub)


def run(login, smoke=False, quotes=False, scanner=False):
    from ib_async import IB, Watchdog

    ib = IB()
    wire(ib)

    port = int(_login_env(login, "API_PORT", str(LOGINS[login]["default_port"])))
    client_id = int(_login_env(login, "API_CLIENT_ID", "17"))
    readonly = _env("IBKR_READONLY", "true").lower() != "false"

    ibc = build_ibc(login)
    dog = Watchdog(ibc, ib, port=port, clientId=client_id,
                   readonly=readonly, appStartupTime=30)

    # Re-open subscriptions on every (re)connect — Watchdog reconnects after the
    # daily Gateway restart, and subscriptions don't survive a socket bounce.
    ib.connectedEvent += lambda: subscribe(ib, quotes=quotes, scanner=scanner)
    ib.disconnectedEvent += lambda: log.warning("[%s] disconnected — Watchdog relaunch",
                                                 login)

    log.info("[%s] starting Watchdog (port=%s clientId=%s readonly=%s)",
             login, port, client_id, readonly)
    dog.start()

    if smoke:
        # Connect, let subscriptions fire, print a snapshot, then exit.
        ib.sleep(15)
        log.info("[%s] SMOKE OK — accounts=%s positions=%d",
                 login, ib.managedAccounts(), len(ib.positions()))
        dog.stop()
        return

    ib.run()


def main():
    ap = argparse.ArgumentParser(description="IBKR real-time streaming daemon")
    ap.add_argument("--login", required=True, choices=sorted(LOGINS),
                    help="which IBKR login/Gateway instance to run")
    ap.add_argument("--smoke", action="store_true",
                    help="connect, print accounts+positions, exit (pipe test)")
    ap.add_argument("--quotes", action="store_true",
                    help="also stream top-of-book quotes on held names")
    ap.add_argument("--scanner", action="store_true",
                    help="also run the starter market scanner")
    args = ap.parse_args()
    run(args.login, smoke=args.smoke, quotes=args.quotes, scanner=args.scanner)


if __name__ == "__main__":
    main()
