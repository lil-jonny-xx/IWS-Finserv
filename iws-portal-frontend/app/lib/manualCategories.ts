// Manual Data categories → dynamic nav tabs.
//
// Categories listed here have NO dedicated section page; once the first entry
// for one of them exists anywhere (any entity), NavTabs shows a tab pointing at
// the generic /assets/<category> page. Categories that already have their own
// page (pms, gold_etf, unlisted, startup, properties, art, bank, forex,
// overseas_equity) are deliberately absent — their entries surface on those
// pages and must not spawn a duplicate tab.
export const DYNAMIC_CATEGORY_LABELS: Record<string, string> = {
  liquid_fund:    'Liquid Funds',
  debt_fund:      'Debt Funds',
  arbitrage_fund: 'Arbitrage Funds',
  ppf:            'PPF',
  direct_equity:  'Direct Equity (Manual)',
  aif:            'AIF',
  overseas_fund:  'Overseas Funds',
  funds_transit:  'Funds in Transit',
  broker_balance: 'Broker Balance',
};
