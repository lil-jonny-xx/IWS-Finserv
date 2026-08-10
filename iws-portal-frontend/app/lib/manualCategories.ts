// Manual Data categories → dynamic nav tabs.
//
// Categories listed here have NO dedicated section page; once the first entry
// for one of them exists anywhere (any entity), NavTabs shows a tab pointing at
// the generic /assets/<category> page. Categories that already have their own
// page (pms, gold_etf, unlisted, startup, properties, art, bank, forex,
// nre_bank, overseas_equity) are deliberately absent — their entries surface on those
// pages and must not spawn a duplicate tab.
//
// direct_equity was removed 2026-07-31 for the same reason: equity entered in the
// /trades register is materialised into equity_holding by manual_positions_worker
// and priced live on the Equity page, so a separate "Direct Equity (Manual)" tab
// would be a second, staler view of the same asset class.
export const DYNAMIC_CATEGORY_LABELS: Record<string, string> = {
  liquid_fund:    'Liquid Funds',
  debt_fund:      'Debt Funds',
  arbitrage_fund: 'Arbitrage Funds',
  ppf:            'PPF',
  aif:            'AIF',
  overseas_fund:  'Overseas Funds',
  funds_transit:  'Funds in Transit',
  broker_balance: 'Broker Balance',
};

// Categories whose generic /assets/<category> page has been retired. The route is a
// catch-all, so dropping the nav tab alone would leave the URL still serving a
// stale second view; these redirect to the page that now owns the asset class.
export const RETIRED_CATEGORY_REDIRECT: Record<string, string> = {
  direct_equity: '/equity',
};
