"""Snapshot-replace ingest of a parsed DBS holdings statement into
foreign_equity_holding (broker='dbs').

A DBS export is a point-in-time holdings snapshot, so each ingest fully REPLACES
that entity's DBS rows — anything absent from the new file is treated as exited.
Native-currency figures come straight from the statement; INR mirrors are native
× that day's fx rate. current_price_native / current_market_value are seeded from
the statement so unresolvable names (SGX etc.) show a real value until — and if —
foreign_price_worker can refresh them live.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from equity import fx

BROKER = "dbs"
TWO = Decimal("0.01")
FOUR = Decimal("0.0001")


def _q(v, quant):
    return None if v is None else Decimal(v).quantize(quant, ROUND_HALF_UP)


def ingest(conn, entity_id: int, parsed: dict, commit: bool = False) -> dict:
    """Replace entity_id's DBS holdings with `parsed['holdings']`.

    Returns a summary dict {replaced, inserted, as_of, fx, unresolved}. When
    commit is False, rolls back (dry-run) so callers can preview safely."""
    cur = conn.cursor()
    as_of = parsed.get("as_of") or date.today()
    holdings = parsed.get("holdings", [])

    # fx per distinct currency, on the statement date (falls back to today).
    fx_cache: dict[str, Decimal] = {}
    for h in holdings:
        ccy = h["currency"]
        if ccy not in fx_cache:
            r = fx.get_rate(conn, ccy, as_of) or fx.get_rate(conn, ccy, date.today())
            fx_cache[ccy] = r if r is not None else Decimal("1")

    cur.execute("SELECT count(*) AS n FROM foreign_equity_holding "
                "WHERE entity_id=%s AND broker=%s", (entity_id, BROKER))
    replaced = cur.fetchone()["n"]
    cur.execute("DELETE FROM foreign_equity_holding WHERE entity_id=%s AND broker=%s",
                (entity_id, BROKER))

    inserted = 0
    unresolved = []
    for h in holdings:
        ccy = h["currency"]
        fxr = fx_cache[ccy]
        qty = Decimal(h["quantity"])
        avg_n = h.get("avg_cost_native")
        px_n = h.get("price_native")
        cost_n = h.get("cost_native") if h.get("cost_native") is not None else (
            (avg_n * qty) if avg_n is not None else None)
        cmv_n = h.get("market_value_native") if h.get("market_value_native") is not None else (
            (px_n * qty) if px_n is not None else None)
        if not h.get("resolvable"):
            unresolved.append(h["symbol"])

        def inr(v):
            return _q(v * fxr, TWO) if v is not None else None

        cur.execute("""
            INSERT INTO foreign_equity_holding
                (entity_id, broker, symbol, isin, exchange, sector, asset_class,
                 currency, fx_rate, quantity,
                 avg_cost_native, cost_native, current_price_native, current_market_value_native,
                 avg_cost, cost, current_price, current_market_value,
                 market_value_as_on, as_of_date, remarks, updated_at)
            VALUES
                (%s,%s,%s,%s,%s,%s,'equity',
                 %s,%s,%s,
                 %s,%s,%s,%s,
                 %s,%s,%s,%s,
                 %s,%s,%s,NOW())
        """, (
            entity_id, BROKER, h["symbol"], h.get("isin"), h.get("exchange"), h.get("sector"),
            ccy, _q(fxr, Decimal("0.000001")), _q(qty, FOUR),
            _q(avg_n, FOUR), _q(cost_n, TWO), _q(px_n, FOUR), _q(cmv_n, TWO),
            _q(inr(avg_n), TWO), inr(cost_n), inr(px_n), inr(cmv_n),
            inr(cmv_n), as_of, h.get("name"),
        ))
        inserted += 1

    cash_summary = _ingest_cash(cur, conn, entity_id, parsed.get("cash", []), as_of, fx_cache)

    summary = {
        "replaced": replaced, "inserted": inserted, "as_of": str(as_of),
        "fx": {k: float(v) for k, v in fx_cache.items()},
        "unresolved": unresolved,
        "cash": cash_summary,
    }
    if commit:
        conn.commit()
    else:
        conn.rollback()
    cur.close()
    return summary


def _ingest_cash(cur, conn, entity_id: int, cash: list, as_of, fx_cache: dict) -> dict:
    """Snapshot-replace the entity's DBS cash into broker_cash (one consolidated
    INR row, dominant currency) + broker_cash_currency (per-currency detail),
    mirroring the IBKR worker. Swept currencies drop off; an all-zero statement
    removes the DBS cash rows entirely. Uses the caller's cursor/txn (so a dry-run
    ingest rolls this back too)."""
    kept, rows, total_inr = [], [], Decimal("0")
    for c in cash:
        ccy = c["currency"]
        native = c.get("market_value_native")
        if native is None or Decimal(native) == 0:
            continue
        native = Decimal(native)
        if ccy not in fx_cache:
            r = fx.get_rate(conn, ccy, as_of) or fx.get_rate(conn, ccy, date.today())
            fx_cache[ccy] = r if r is not None else Decimal("1")
        fxr = fx_cache[ccy]
        inr = _q(native * fxr, TWO)
        total_inr += inr
        kept.append(ccy)
        rows.append((ccy, native, inr, fxr))
        cur.execute("""
            INSERT INTO broker_cash_currency
                (entity_id, broker, currency, balance_native, balance_inr, fx_rate, as_of_date, updated_at)
            VALUES (%s, 'dbs', %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, broker, currency) DO UPDATE SET
                balance_native = EXCLUDED.balance_native,
                balance_inr    = EXCLUDED.balance_inr,
                fx_rate        = EXCLUDED.fx_rate,
                as_of_date     = EXCLUDED.as_of_date,
                updated_at     = NOW()
        """, (entity_id, ccy, float(native), float(inr), float(fxr), as_of))

    # Snapshot semantics: drop DBS currencies no longer present.
    if kept:
        cur.execute("DELETE FROM broker_cash_currency "
                    "WHERE entity_id=%s AND broker='dbs' AND currency <> ALL(%s)",
                    (entity_id, kept))
    else:
        cur.execute("DELETE FROM broker_cash_currency WHERE entity_id=%s AND broker='dbs'",
                    (entity_id,))

    if rows:
        dom = max(rows, key=lambda r: abs(r[2]))   # dominant by INR value
        dom_ccy, dom_native, _dom_inr, dom_fxr = dom
        cur.execute("""
            INSERT INTO broker_cash
                (entity_id, broker, balance, currency, fx_rate, balance_native, as_of_date, updated_at)
            VALUES (%s, 'dbs', %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (entity_id, broker) DO UPDATE SET
                balance        = EXCLUDED.balance,
                currency       = EXCLUDED.currency,
                fx_rate        = EXCLUDED.fx_rate,
                balance_native = EXCLUDED.balance_native,
                as_of_date     = EXCLUDED.as_of_date,
                updated_at     = NOW()
        """, (entity_id, float(total_inr), dom_ccy, float(dom_fxr), float(dom_native), as_of))
    else:
        cur.execute("DELETE FROM broker_cash WHERE entity_id=%s AND broker='dbs'", (entity_id,))

    return {"currencies": {ccy: float(inr) for ccy, _n, inr, _f in rows},
            "total_inr": float(total_inr)}
