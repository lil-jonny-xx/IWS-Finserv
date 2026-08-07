'use client';
import { useEffect, useState, useMemo, Fragment } from 'react';
import { useRouter } from 'next/navigation';
import { Glass } from '@/app/components/PrivacyGlass';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface RealisedRow {
  entity: string;
  broker?: string | null;  // set only in the demat (group=broker) fetch; null for MF/PMS/real estate
  category: string;
  group: string;
  security_name: string;
  purchase_amount: number | null;
  sale_date: string;
  sale_amount: number | null;
  pnl: number | null;
  st_pnl: number | null;   // Indian equity only: short-term slice (held ≤ 12 months)
  lt_pnl: number | null;   // Indian equity only: long-term slice (held > 12 months)
  return_pct: number | null;
  is_statement?: boolean;  // synthetic row injected by the backend so totals defer to a
                           // broker P&L statement ('Broker statement adj.' / 'Derivatives')
}

// Demat/broker display names. Anything without a demat (MF, PMS, real estate) is
// bucketed under this dash so the demat views still account for every rupee.
const NO_DEMAT = 'No demat (MF / PMS / property)';
const BROKER_LABEL: Record<string, string> = {
  zerodha: 'Zerodha', angel_one: 'Angel One', dhan: 'Dhan',
  ibkr: 'Interactive Brokers', vested: 'Vested', dbs: 'DBS Wealth',
};
function dematLabel(b: string | null | undefined): string {
  if (!b) return NO_DEMAT;
  return BROKER_LABEL[b] ?? b;
}

// Section order on the page; any category not listed falls in after these, sorted.
// Commodities is not a `category` the API returns — it is a `group`, carried on rows
// the backend classified as gold/silver (ETFs, SGBs, gold funds). It is promoted to a
// section of its own here so the page splits the same way the XLSX realised sheet
// does, instead of burying precious metals inside Equity and Mutual Funds.
const CATEGORY_ORDER = ['Equity', 'Commodities', 'Derivatives', 'Mutual Funds', 'Foreign Equity', 'PMS', 'Broker statement adj.'];
const GROUP_AS_SECTION = new Set(['Commodities']);

// Section names shortened for the year-on-year asset-class toggle, which sits in a
// row of other controls and has to stay narrow. Anything unlisted shows in full.
const YOY_CAT_LABEL: Record<string, string> = {
  'Mutual Funds': 'Mutual funds',
  'Foreign Equity': 'Foreign',
  'Broker statement adj.': 'Stmt adj.',
};

// The asset section a row belongs to. Shared by the detail sections and the
// year-on-year filter so both split the data the same way — a gold/silver row is
// promoted out of Equity/Mutual Funds by its `group` in exactly one place.
function sectionOf(r: RealisedRow): string {
  return (r.group && GROUP_AS_SECTION.has(r.group))
    ? r.group
    : (r.category || r.group || 'Other');
}

function inr(v: number | null): string {
  if (v == null) return '—';
  return v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}
function pct(v: number | null): string {
  if (v == null) return '—';
  return `${(v * 100).toFixed(2)}%`;
}

// Indian financial year label for a sale date: FY runs Apr–Mar, so a sale on
// 2026-06-19 belongs to FY 2026-27 and one on 2026-02-18 to FY 2025-26. Bucketing
// on the calendar year instead would split every FY across two rows and make the
// year-on-year view disagree with the tax view of the same trades.
function fyOf(saleDate: string): string {
  if (!saleDate) return '—';
  const [y, m] = saleDate.split('-').map(Number);
  if (!y || !m) return '—';
  const start = m >= 4 ? y : y - 1;
  return `${start}-${String(start + 1).slice(-2)}`;
}

function pnlStyle(v: number | null) {
  if (v == null) return { color: 'var(--ghost)' };
  return { color: v >= 0 ? 'var(--gain)' : 'var(--peril)' };
}

function Toggle<T extends string>({ label, value, onChange, options }: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: { v: T; label: string }[];
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs font-medium" style={{ color: 'var(--ghost)' }}>{label}</span>
      <div className="inline-flex rounded-lg overflow-hidden" style={{ border: '1px solid var(--rule)' }}>
        {options.map(o => {
          const active = o.v === value;
          return (
            <button
              key={o.v}
              onClick={() => onChange(o.v)}
              className="px-3 py-1 text-xs font-medium transition-colors"
              style={{
                background: active ? 'var(--prime)' : 'var(--card)',
                color: active ? '#fff' : 'var(--dim)',
              }}
            >{o.label}</button>
          );
        })}
      </div>
    </div>
  );
}

type Period = 'fy' | 'inception';
type Switches = 'include' | 'exclude';
type View = 'detail' | 'entity' | 'yoy' | 'demat' | 'dividends';
type DematBreak = 'total' | 'entity' | 'yoy' | 'entity_yoy';   // sub-view inside the demat tab

interface DividendRow {
  entity: string;
  security_name: string;
  ex_date: string;
  quantity: number;
  rate_per_share: number;
  amount: number;
  fy: string;
  variance_pct: number | null;   // set once validated against a broker report
  feed: string | null;
}
interface DividendCoverage {
  resolved: number;
  unresolved: number;
  unresolved_symbols: string[];
}

// An imported broker P&L statement (the per-scrip realised oracle). segment_totals
// carries the broker's own FY realised per segment (EQ delivery, FnO); we overlay the
// EQ figure as authoritative on the By-demat view and surface FnO as Derivatives.
interface StmtRow {
  id: number;
  entity_id: number;
  entity_name: string;
  broker: string;
  client_id: string | null;
  fy_label: string | null;
  segment_totals: Record<string, { realised?: number } & Record<string, number>>;
  n_lines: number;
  created_at: string;
}
function stmtEq(s: StmtRow): number { return Number(s.segment_totals?.EQ?.realised ?? 0); }
function stmtFno(s: StmtRow): number { return Number(s.segment_totals?.FnO?.realised ?? 0); }

export default function RealisedGainsPage() {
  const router = useRouter();
  const [rows, setRows] = useState<RealisedRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [period, setPeriod] = useState<Period>('fy');
  const [switches, setSwitches] = useState<Switches>('include');
  const [view, setView] = useState<View>('detail');
  // Asset class shown in the year-on-year matrix. That view sums every category into
  // one figure per (FY, entity), which hides smaller books completely — mutual funds
  // are a few lakh sitting inside crores of equity. 'all' keeps the old behaviour.
  const [yoyCat, setYoyCat] = useState<string>('all');

  const [divRows, setDivRows] = useState<DividendRow[]>([]);
  const [divCov, setDivCov] = useState<DividendCoverage | null>(null);

  // Demat (group=broker) rows are a separate fetch: FIFO is re-matched per broker,
  // so these numbers can differ slightly from the per-entity rows for a stock held
  // at two brokers. Refetched when the Period/Switches toggles change.
  const [dematRows, setDematRows] = useState<RealisedRow[]>([]);
  const [dematBreak, setDematBreak] = useState<DematBreak>('total');
  const [dematLoaded, setDematLoaded] = useState(false);

  // Broker P&L statement overlay (admin only). Fetched while the demat tab is open;
  // used to show, per broker × FY, the broker's own realised (authoritative) beside
  // our FIFO, plus any F&O realised as Derivatives.
  const [isAdmin, setIsAdmin] = useState(false);
  const [stmts, setStmts] = useState<StmtRow[]>([]);
  const [showImport, setShowImport] = useState(false);
  const reloadStmts = () => {
    fetch(`${API_URL}/api/v1/realised-gains/pnl-statement`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : []))
      .then((j: StmtRow[]) => setStmts(Array.isArray(j) ? j : []))
      .catch(() => {});
  };

  // Year-on-year is meaningless against a single FY, so that view always pulls the
  // whole history regardless of the Period toggle (which is hidden while it's active).
  const effectivePeriod: Period = view === 'yoy' ? 'inception' : period;

  // Role only decides which nav tabs show (Manual Data is admin-only); the report
  // data itself is available to every authenticated user.
  useEffect(() => {
    const c = new AbortController();
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include', signal: c.signal })
      .then(r => r.ok ? r.json() : null)
      .then((u: { role?: string } | null) => {
        if (!u) { router.replace('/'); return; }
        setIsAdmin(u.role === 'admin');
      })
      .catch(err => { if (err.name !== 'AbortError') router.replace('/'); });
    return () => c.abort();
  }, [router]);

  // Pull the imported statements once the demat tab is open and the user is an admin.
  // The list endpoint is admin-only, so non-admins simply never see the overlay.
  useEffect(() => {
    if (view !== 'demat' || !isAdmin) return;
    reloadStmts();
  }, [view, isAdmin]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const res = await fetch(
        `${API_URL}/api/v1/realised-gains?period=${effectivePeriod}&switches=${switches}`,
        { credentials: 'include' },
      );
      if (cancelled) return;
      if (res.status === 401) { router.push('/'); return; }
      if (res.ok) setRows(await res.json());
      setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [router, effectivePeriod, switches]);

  // Dividends are a separate feed on the same page — fetched only when that view is
  // opened, and only once, since the figures are recomputed by a weekly worker rather
  // than changing with the Period/Switches toggles.
  useEffect(() => {
    if (view !== 'dividends' || divCov !== null) return;
    let cancelled = false;
    (async () => {
      const res = await fetch(`${API_URL}/api/v1/dividends?period=inception`,
                              { credentials: 'include' });
      if (cancelled) return;
      if (res.status === 401) { router.push('/'); return; }
      if (res.ok) {
        const j = await res.json();
        setDivRows(j.rows ?? []);
        setDivCov(j.coverage ?? { resolved: 0, unresolved: 0, unresolved_symbols: [] });
      }
    })();
    return () => { cancelled = true; };
  }, [view, divCov, router]);

  // Demat/broker rows — fetched only while the demat tab is open. Its year-on-year
  // sub-view always spans the whole history (like the main YoY), so it pulls
  // inception regardless of the Period toggle.
  const dematPeriod: Period = (dematBreak === 'yoy' || dematBreak === 'entity_yoy') ? 'inception' : period;
  useEffect(() => {
    if (view !== 'demat') return;
    let cancelled = false;
    setDematLoaded(false);
    (async () => {
      const res = await fetch(
        `${API_URL}/api/v1/realised-gains?group=broker&period=${dematPeriod}&switches=${switches}`,
        { credentials: 'include' },
      );
      if (cancelled) return;
      if (res.status === 401) { router.push('/'); return; }
      if (res.ok) setDematRows(await res.json());
      setDematLoaded(true);
    })();
    return () => { cancelled = true; };
  }, [view, dematPeriod, switches, router]);

  // Show the loading state while a toggle change refetches.
  const changePeriod = (p: Period) => { setLoading(true); setPeriod(p); };
  const changeSwitches = (s: Switches) => { setLoading(true); setSwitches(s); };

  const totalPnl = rows.reduce((s, r) => s + (r.pnl ?? 0), 0);
  // Short/long-term totals only exist for Indian equity (FIFO-split); other
  // categories carry null, so they don't contribute.
  const totalSt = rows.reduce((s, r) => s + (r.st_pnl ?? 0), 0);
  const totalLt = rows.reduce((s, r) => s + (r.lt_pnl ?? 0), 0);
  const hasStLt = rows.some(r => r.st_pnl != null || r.lt_pnl != null);

  // Split rows into asset sections (Equity / Mutual Funds / …). In "since inception"
  // mode each section is ordered latest sale first; FY-to-date keeps the backend's
  // chronological order.
  const sections = useMemo(() => {
    const map = new Map<string, RealisedRow[]>();
    for (const r of rows) {
      // A gold/silver row keeps its own category for every other view, but here the
      // group wins so it lands in the Commodities section rather than under Equity.
      const c = sectionOf(r);
      if (!map.has(c)) map.set(c, []);
      map.get(c)!.push(r);
    }
    const ordered = [
      ...CATEGORY_ORDER.filter(c => map.has(c)),
      ...[...map.keys()].filter(c => !CATEGORY_ORDER.includes(c)).sort(),
    ];
    return ordered.map(cat => {
      let rs = map.get(cat)!;
      if (period === 'inception') {
        rs = [...rs].sort((a, b) => b.sale_date.localeCompare(a.sale_date));
      }
      return { cat, rows: rs };
    });
  }, [rows, period]);

  // Per-entity totals. The API already returns `entity` on every row (visibility is
  // uniform), so this is a pure regroup of the same data the detail table shows —
  // no extra request, and it always agrees with the headline total.
  const byEntity = useMemo(() => {
    const m = new Map<string, { pnl: number; st: number; lt: number; n: number; hasStLt: boolean }>();
    for (const r of rows) {
      const k = r.entity || '—';
      const cur = m.get(k) ?? { pnl: 0, st: 0, lt: 0, n: 0, hasStLt: false };
      cur.pnl += r.pnl ?? 0;
      cur.st  += r.st_pnl ?? 0;
      cur.lt  += r.lt_pnl ?? 0;
      cur.n   += 1;
      // ST/LT exist only for Indian equity; an entity holding only MF/PMS shows a
      // dash rather than a misleading ₹0.
      if (r.st_pnl != null || r.lt_pnl != null) cur.hasStLt = true;
      m.set(k, cur);
    }
    return [...m.entries()].sort((a, b) => b[1].pnl - a[1].pnl);
  }, [rows]);

  // Asset classes present in the data, for the year-on-year filter. Driven by the
  // rows themselves so a category the backend starts returning shows up on its own.
  const yoyCats = useMemo(() => {
    const present = new Set(rows.map(sectionOf));
    return [
      ...CATEGORY_ORDER.filter(c => present.has(c)),
      ...[...present].filter(c => !CATEGORY_ORDER.includes(c)).sort(),
    ];
  }, [rows]);

  // FY × entity matrix for the year-on-year view, for one asset class or all of them.
  // Entity columns follow the filtered rows, so picking Mutual Funds drops the
  // entities that never held any instead of showing a row of dashes.
  const yoy = useMemo(() => {
    const src = yoyCat === 'all' ? rows : rows.filter(r => sectionOf(r) === yoyCat);
    const ents = [...new Set(src.map(r => r.entity || '—'))].sort();
    const grid = new Map<string, Map<string, number>>();
    for (const r of src) {
      const fy = fyOf(r.sale_date);
      if (fy === '—') continue;            // a row with no sale date can't be dated
      if (!grid.has(fy)) grid.set(fy, new Map());
      const em = grid.get(fy)!;
      const k = r.entity || '—';
      em.set(k, (em.get(k) ?? 0) + (r.pnl ?? 0));
    }
    const fys = [...grid.keys()].sort().reverse();   // most recent FY first
    const cell = (fy: string, e: string) => grid.get(fy)?.get(e) ?? 0;
    const rowTotal = (fy: string) =>
      [...(grid.get(fy)?.values() ?? [])].reduce((s, v) => s + v, 0);
    const colTotal = (e: string) => fys.reduce((s, fy) => s + cell(fy, e), 0);
    // The grand total must follow the filter too, or the corner cell contradicts
    // every column above it.
    const grand = src.reduce((s, r) => s + (r.pnl ?? 0), 0);
    return { ents, fys, cell, rowTotal, colTotal, grand };
  }, [rows, yoyCat]);

  // Dividends as an FY × entity matrix — the same shape as the realised YoY view, so
  // the two read the same way. The backend already stamps each row with its Indian FY.
  const divYoy = useMemo(() => {
    const ents = [...new Set(divRows.map(r => r.entity || '—'))].sort();
    const grid = new Map<string, Map<string, number>>();
    for (const r of divRows) {
      if (!grid.has(r.fy)) grid.set(r.fy, new Map());
      const em = grid.get(r.fy)!;
      const k = r.entity || '—';
      em.set(k, (em.get(k) ?? 0) + r.amount);
    }
    const fys = [...grid.keys()].sort().reverse();
    const cell = (fy: string, e: string) => grid.get(fy)?.get(e) ?? 0;
    const rowTotal = (fy: string) => [...(grid.get(fy)?.values() ?? [])].reduce((s, v) => s + v, 0);
    const colTotal = (e: string) => fys.reduce((s, fy) => s + cell(fy, e), 0);
    const total = divRows.reduce((s, r) => s + r.amount, 0);
    return { ents, fys, cell, rowTotal, colTotal, total };
  }, [divRows]);

  // ── Demat views ──────────────────────────────────────────────────────────────
  const dematTotal = dematRows.reduce((s, r) => s + (r.pnl ?? 0), 0);

  // By demat: one row per broker, with ST/LT where the broker carries Indian equity.
  const byDemat = useMemo(() => {
    const m = new Map<string, { pnl: number; st: number; lt: number; n: number; hasStLt: boolean }>();
    for (const r of dematRows) {
      const k = dematLabel(r.broker);
      const cur = m.get(k) ?? { pnl: 0, st: 0, lt: 0, n: 0, hasStLt: false };
      cur.pnl += r.pnl ?? 0;
      cur.st  += r.st_pnl ?? 0;
      cur.lt  += r.lt_pnl ?? 0;
      cur.n   += 1;
      if (r.st_pnl != null || r.lt_pnl != null) cur.hasStLt = true;
      m.set(k, cur);
    }
    // The no-demat bucket always sorts last; real brokers by P&L desc.
    return [...m.entries()].sort((a, b) => {
      if (a[0] === NO_DEMAT) return 1;
      if (b[0] === NO_DEMAT) return -1;
      return b[1].pnl - a[1].pnl;
    });
  }, [dematRows]);

  // Demat × entity matrix (broker rows, entity columns).
  const dematByEntity = useMemo(() => {
    const brokers = [...new Set(dematRows.map(r => dematLabel(r.broker)))]
      .sort((a, b) => (a === NO_DEMAT ? 1 : b === NO_DEMAT ? -1 : a.localeCompare(b)));
    const ents = [...new Set(dematRows.map(r => r.entity || '—'))].sort();
    const grid = new Map<string, Map<string, number>>();
    for (const r of dematRows) {
      const b = dematLabel(r.broker);
      if (!grid.has(b)) grid.set(b, new Map());
      const em = grid.get(b)!;
      const k = r.entity || '—';
      em.set(k, (em.get(k) ?? 0) + (r.pnl ?? 0));
    }
    const cell = (b: string, e: string) => grid.get(b)?.get(e) ?? 0;
    const rowTotal = (b: string) => [...(grid.get(b)?.values() ?? [])].reduce((s, v) => s + v, 0);
    const colTotal = (e: string) => brokers.reduce((s, b) => s + cell(b, e), 0);
    return { brokers, ents, cell, rowTotal, colTotal };
  }, [dematRows]);

  // Demat × financial-year matrix (broker rows, FY columns).
  const dematByYoy = useMemo(() => {
    const brokers = [...new Set(dematRows.map(r => dematLabel(r.broker)))]
      .sort((a, b) => (a === NO_DEMAT ? 1 : b === NO_DEMAT ? -1 : a.localeCompare(b)));
    const grid = new Map<string, Map<string, number>>();
    for (const r of dematRows) {
      const fy = fyOf(r.sale_date);
      if (fy === '—') continue;
      const b = dematLabel(r.broker);
      if (!grid.has(b)) grid.set(b, new Map());
      const fm = grid.get(b)!;
      fm.set(fy, (fm.get(fy) ?? 0) + (r.pnl ?? 0));
    }
    const fys = [...new Set([...grid.values()].flatMap(m => [...m.keys()]))].sort().reverse();
    const cell = (b: string, fy: string) => grid.get(b)?.get(fy) ?? 0;
    const rowTotal = (b: string) => [...(grid.get(b)?.values() ?? [])].reduce((s, v) => s + v, 0);
    const colTotal = (fy: string) => brokers.reduce((s, b) => s + cell(b, fy), 0);
    return { brokers, fys, cell, rowTotal, colTotal };
  }, [dematRows]);

  // Entity × broker × financial-year — the full three-way breakdown. Rows are
  // (entity, broker) pairs grouped under their entity with a per-entity subtotal;
  // columns are FYs. Same source rows as the other demat views, just pivoted on all
  // three keys at once.
  const dematByEntityYoy = useMemo(() => {
    const grid = new Map<string, Map<string, number>>();   // `${entity}||${broker}` → fy → pnl
    for (const r of dematRows) {
      const rk = `${r.entity || '—'}||${dematLabel(r.broker)}`;
      const fy = fyOf(r.sale_date);
      if (fy === '—') continue;
      if (!grid.has(rk)) grid.set(rk, new Map());
      const fm = grid.get(rk)!;
      fm.set(fy, (fm.get(fy) ?? 0) + (r.pnl ?? 0));
    }
    const fys = [...new Set([...grid.values()].flatMap(m => [...m.keys()]))].sort().reverse();
    const cell = (rk: string, fy: string) => grid.get(rk)?.get(fy) ?? 0;
    const rowTotal = (rk: string) => [...(grid.get(rk)?.values() ?? [])].reduce((s, v) => s + v, 0);
    // Entities in P&L order; each carries its (entity, broker) row keys, brokers last
    // if it's the no-demat bucket.
    const entities = [...new Set([...grid.keys()].map(k => k.split('||')[0]))].sort();
    const rowsFor = (ent: string) => [...grid.keys()]
      .filter(k => k.split('||')[0] === ent)
      .sort((a, b) => {
        const ba = a.split('||')[1], bb = b.split('||')[1];
        return ba === NO_DEMAT ? 1 : bb === NO_DEMAT ? -1 : ba.localeCompare(bb);
      })
      .map(rk => ({ rk, broker: rk.split('||')[1] }));
    const entTotal = (ent: string, fy: string) =>
      rowsFor(ent).reduce((s, r) => s + cell(r.rk, fy), 0);
    const colTotal = (fy: string) => [...grid.keys()].reduce((s, rk) => s + cell(rk, fy), 0);
    const grand = fys.reduce((s, fy) => s + colTotal(fy), 0);
    return { entities, rowsFor, fys, cell, rowTotal, entTotal, colTotal, grand };
  }, [dematRows]);

  // Reconciliation of our FIFO against the imported broker statements, per broker × FY.
  // The statement's EQ realised is the broker's own authority; a non-zero variance is
  // what our engine still can't explain (a residual corporate action / missing trade).
  // FnO realised is broker-only (we have no F&O engine) and shown as Derivatives.
  const fyKey = (fy: string) => {           // '2024-25' → 'FY24-25' to match backend labels
    const [a, b] = fy.split('-');
    return a && b ? `FY${a.slice(-2)}-${b}` : fy;
  };
  const recon = useMemo(() => {
    if (!isAdmin || stmts.length === 0) return null;
    const our = new Map<string, number>();
    for (const r of dematRows) {
      if (!r.broker || r.is_statement) continue;   // raw FIFO only — exclude the override rows
      const k = `${r.broker}|${fyKey(fyOf(r.sale_date))}`;
      our.set(k, (our.get(k) ?? 0) + (r.pnl ?? 0));
    }
    const eq = new Map<string, number>(), fno = new Map<string, number>();
    for (const s of stmts) {
      if (!s.fy_label) continue;
      const k = `${s.broker}|${s.fy_label}`;
      eq.set(k, (eq.get(k) ?? 0) + stmtEq(s));
      fno.set(k, (fno.get(k) ?? 0) + stmtFno(s));
    }
    const rows = [...eq.keys()].sort().map(k => {
      const [broker, fy] = k.split('|');
      const ourPnl = our.get(k) ?? 0, stmtPnl = eq.get(k) ?? 0;
      return { broker, fy, ourPnl, stmtPnl, variance: ourPnl - stmtPnl, fnoPnl: fno.get(k) ?? 0 };
    });
    const fnoTotal = rows.reduce((s, r) => s + r.fnoPnl, 0);
    return { rows, fnoTotal };
  }, [isAdmin, stmts, dematRows]);

  return (
    <div className="min-h-screen" style={{ background: 'var(--page)' }}>
      {/* Section tabs are global — see components/GlobalNav in the root layout. */}
      {showImport && (
        <ImportPnlModal
          onClose={() => setShowImport(false)}
          onCommitted={() => { setShowImport(false); reloadStmts(); }}
        />
      )}

      <main className="shell py-6">
        <div className="mb-4 flex items-end justify-between">
          <div>
            <h1 className="text-lg font-bold" style={{ color: 'var(--ink)' }}>
              {view === 'dividends' ? 'Dividends (all years)'
                : view === 'demat' ? `Realised Gains by demat (${
                    dematBreak === 'yoy' ? 'year on year'
                      : dematBreak === 'entity_yoy' ? 'entity × broker × year'
                      : dematPeriod === 'inception' ? 'since inception' : 'FY to date'})`
                : `Realised Gains (${
                    view === 'yoy' ? 'year on year'
                      : effectivePeriod === 'inception' ? 'since inception' : 'FY to date'})`}
            </h1>
            {view === 'dividends' ? (
              <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                Indian dividends are paid straight to your bank, never through the broker, so these are
                <strong> derived</strong>: ex-date and rate per share from market data, multiplied by the
                quantity the trade ledger says was held on that date. Figures are <strong>gross</strong> —
                dividends over ₹5,000 a year attract 10% TDS, so the amount credited is lower.
              </p>
            ) : (
              <p className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                MF realised auto-computed from CAS transactions. Indian equity is FIFO-matched (broker/tax
                basis), gross of charges, split into short-term (held ≤ 12 months) and long-term (&gt; 12 months).
                {' '}Switches are {switches === 'exclude' ? 'excluded' : 'included'}.
              </p>
            )}
          </div>
          {/* Narrow block, so the pane gets a little breathing room around the
              figures rather than clamping to the text. */}
          {view === 'dividends' ? (
            <Glass label="Dividends" className="shrink-0">
              <div className="text-right px-2 py-1">
                <div className="text-xs" style={{ color: 'var(--ghost)' }}>Total dividends (gross)</div>
                <div className="text-base font-bold" style={{ color: 'var(--gain)' }}>₹{inr(divYoy.total)}</div>
                <div className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                  {divRows.length} payment{divRows.length === 1 ? '' : 's'}
                </div>
              </div>
            </Glass>
          ) : (
          <Glass label="P&amp;L" className="shrink-0">
          <div className="text-right px-2 py-1">
            <div className="text-xs" style={{ color: 'var(--ghost)' }}>
              {view === 'demat' ? 'Total realised P&L (by demat)'
                : view === 'yoy' && yoyCat !== 'all' ? `Total realised P&L (${YOY_CAT_LABEL[yoyCat] ?? yoyCat})`
                : 'Total realised P&L'}
            </div>
            {/* Follows the YoY asset-class filter, so the headline can never
                contradict the grand total in the corner of the matrix below it. */}
            {(() => { const hp = view === 'demat' ? dematTotal : view === 'yoy' ? yoy.grand : totalPnl; return (
            <div className="text-base font-bold" style={{ color: hp >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
              ₹{inr(hp)}
            </div>
            ); })()}
            {/* ST/LT is computed across every row, so it would describe the wrong
                book once the YoY matrix is filtered to one asset class — and mutual
                funds carry no ST/LT split at all. Drop it rather than mislead. */}
            {hasStLt && view !== 'demat' && !(view === 'yoy' && yoyCat !== 'all') && (
              <div className="text-xs mt-0.5" style={{ color: 'var(--ghost)' }}>
                <span style={{ color: totalSt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>ST ₹{inr(totalSt)}</span>
                {' · '}
                <span style={{ color: totalLt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>LT ₹{inr(totalLt)}</span>
              </div>
            )}
          </div>
          </Glass>
          )}
        </div>

        <div className="mb-6 flex flex-wrap items-center gap-x-6 gap-y-2">
          <Toggle<View>
            label="View"
            value={view}
            // Only flip to Loading when the switch actually changes what gets fetched.
            // Detail / By entity / Dividends all reuse data already in state, so
            // setting it unconditionally left the page stuck on "Loading…" — the
            // effect below never re-ran to clear it.
            onChange={(v) => {
              const nextPeriod: Period = v === 'yoy' ? 'inception' : period;
              if (nextPeriod !== effectivePeriod) setLoading(true);
              setView(v);
            }}
            options={[
              { v: 'detail',    label: 'Detail' },
              { v: 'entity',    label: 'By entity' },
              { v: 'yoy',       label: 'Year on year' },
              { v: 'demat',     label: 'By demat' },
              { v: 'dividends', label: 'Dividends' },
            ]}
          />
          {/* Demat sub-view: total per broker, or crossed with entity / financial year. */}
          {view === 'demat' && (
            <Toggle<DematBreak>
              label="Break by"
              value={dematBreak}
              onChange={setDematBreak}
              options={[
                { v: 'total',      label: 'Total' },
                { v: 'entity',     label: 'By entity' },
                { v: 'yoy',        label: 'Year on year' },
                { v: 'entity_yoy', label: 'Entity × year' },
              ]}
            />
          )}
          {/* Asset class for the YoY matrix. Only worth showing once there is more
              than one class to choose between. */}
          {view === 'yoy' && yoyCats.length > 1 && (
            <Toggle<string>
              label="Asset class"
              value={yoyCat}
              onChange={setYoyCat}
              options={[
                { v: 'all', label: 'All' },
                ...yoyCats.map(c => ({ v: c, label: YOY_CAT_LABEL[c] ?? c })),
              ]}
            />
          )}
          {/* Hidden rather than disabled in the YoY view: that view always spans the
              whole history, so offering a period control that does nothing would be
              worse than not offering one. */}
          {view !== 'yoy' && view !== 'dividends' && !(view === 'demat' && (dematBreak === 'yoy' || dematBreak === 'entity_yoy')) && (
            <Toggle<Period>
              label="Period"
              value={period}
              onChange={changePeriod}
              options={[{ v: 'fy', label: 'FY to date' }, { v: 'inception', label: 'Since inception' }]}
            />
          )}
          {view !== 'dividends' && (
          <Toggle<Switches>
            label="Switches"
            value={switches}
            onChange={changeSwitches}
            options={[{ v: 'include', label: 'Include' }, { v: 'exclude', label: 'Exclude' }]}
          />
          )}
          {/* Admin-only: import a broker P&L statement to reconcile against (By-demat). */}
          {view === 'demat' && isAdmin && (
            <button
              onClick={() => setShowImport(true)}
              className="ml-auto px-3 py-1 text-xs font-medium rounded-lg transition-colors"
              style={{ background: 'var(--prime)', color: '#fff' }}
            >Import P&amp;L statement</button>
          )}
        </div>

        {view === 'dividends' ? (
          divCov === null ? (
            <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
          ) : (
          <>
            {/* Coverage is stated up front, not buried. A scrip with no market-data
                ticker contributes zero, so without this line an under-count would be
                indistinguishable from a genuinely dividend-free portfolio. */}
            {divCov.unresolved > 0 && (
              <div className="mb-4 px-3 py-2 rounded-lg text-xs"
                   style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                Matched {divCov.resolved} of {divCov.resolved + divCov.unresolved} securities to a market-data
                feed. {divCov.unresolved} could not be matched and contribute nothing — typically SME-board
                scrips, sovereign gold bonds (which pay interest, not dividends) and renamed tickers.
                {divCov.unresolved_symbols.length > 0 && (
                  <span style={{ color: 'var(--ghost)' }}>
                    {' '}Unmatched: {divCov.unresolved_symbols.slice(0, 12).join(', ')}
                    {divCov.unresolved_symbols.length > 12 ? ` +${divCov.unresolved_symbols.length - 12} more` : ''}.
                  </span>
                )}
              </div>
            )}
            {divRows.length === 0 ? (
              <div className="py-16 text-center rounded-xl"
                   style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
                <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No dividends computed yet</p>
                <p className="text-xs" style={{ color: 'var(--ghost)' }}>
                  Run the dividend worker to derive them from the trade ledger.
                </p>
              </div>
            ) : (
              <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                      <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Financial year</th>
                      {divYoy.ents.map(e => (
                        <th key={e} className="px-3 py-2 text-right font-semibold whitespace-nowrap">{e}</th>
                      ))}
                      <th className="px-3 py-2 text-right font-semibold">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {divYoy.fys.map(fy => (
                      <tr key={fy} style={{ borderTop: '1px solid var(--rule)' }}>
                        <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>FY {fy}</td>
                        {divYoy.ents.map(e => {
                          const v = divYoy.cell(fy, e);
                          return (
                            <td key={e} className="px-3 py-2 text-right"
                                style={{ color: v === 0 ? 'var(--ghost)' : 'var(--gain)' }}>
                              {v === 0 ? '—' : `₹${inr(v)}`}
                            </td>
                          );
                        })}
                        <td className="px-3 py-2 text-right font-medium" style={{ color: 'var(--gain)' }}>
                          ₹{inr(divYoy.rowTotal(fy))}
                        </td>
                      </tr>
                    ))}
                    <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                      <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>All years</td>
                      {divYoy.ents.map(e => (
                        <td key={e} className="px-3 py-2 text-right font-bold" style={{ color: 'var(--gain)' }}>
                          ₹{inr(divYoy.colTotal(e))}
                        </td>
                      ))}
                      <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--gain)' }}>
                        ₹{inr(divYoy.total)}
                      </td>
                    </tr>
                  </tbody>
                </table>
                </div>
              </div>
            )}
          </>
          )
        ) : view === 'demat' ? (
          !dematLoaded ? (
            <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
          ) : dematRows.length === 0 ? (
            <div className="py-16 text-center rounded-xl" style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
              <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No realised gains to break down by demat</p>
              <p className="text-xs" style={{ color: 'var(--ghost)' }}>They appear as stocks are sold across your broker accounts.</p>
            </div>
          ) : (
          <>
            <p className="text-xs mb-3" style={{ color: 'var(--ghost)' }}>
              Realised P&amp;L is re-matched FIFO within each demat, so a stock held at two
              brokers is netted against its own lots at each — these figures can differ
              slightly from the per-entity view. Mutual funds, PMS and property have no
              demat and sit under &ldquo;{NO_DEMAT}&rdquo;.
            </p>
            {dematBreak === 'total' ? (
              <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                      <th className="px-3 py-2 text-left font-semibold">Demat / broker</th>
                      <th className="px-3 py-2 text-right font-semibold">Sales</th>
                      <th className="px-3 py-2 text-right font-semibold">Realised P&amp;L</th>
                      <th className="px-3 py-2 text-right font-semibold">Short-term</th>
                      <th className="px-3 py-2 text-right font-semibold">Long-term</th>
                      <th className="px-3 py-2 text-right font-semibold">Share</th>
                    </tr>
                  </thead>
                  <tbody>
                    {byDemat.map(([b, v]) => (
                      <tr key={b} style={{ borderTop: '1px solid var(--rule)' }}>
                        <td className="px-3 py-2 font-medium" style={{ color: 'var(--ink)' }}>{b}</td>
                        <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>{v.n}</td>
                        <td className="px-3 py-2 text-right font-medium" style={pnlStyle(v.pnl)}>₹{inr(v.pnl)}</td>
                        <td className="px-3 py-2 text-right" style={pnlStyle(v.hasStLt ? v.st : null)}>
                          {v.hasStLt ? `₹${inr(v.st)}` : '—'}</td>
                        <td className="px-3 py-2 text-right" style={pnlStyle(v.hasStLt ? v.lt : null)}>
                          {v.hasStLt ? `₹${inr(v.lt)}` : '—'}</td>
                        <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>
                          {(() => {
                            const denom = byDemat.reduce((s, [, x]) => s + Math.abs(x.pnl), 0);
                            return denom ? `${((Math.abs(v.pnl) / denom) * 100).toFixed(1)}%` : '—';
                          })()}
                        </td>
                      </tr>
                    ))}
                    <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                      <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>Total</td>
                      <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--dim)' }}>{dematRows.length}</td>
                      <td className="px-3 py-2 text-right font-bold" style={pnlStyle(dematTotal)}>₹{inr(dematTotal)}</td>
                      <td className="px-3 py-2" /><td className="px-3 py-2" />
                      <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--dim)' }}>100%</td>
                    </tr>
                  </tbody>
                </table>
                </div>
              </div>
            ) : dematBreak === 'entity' ? (
              <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                      <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Demat / broker</th>
                      {dematByEntity.ents.map(e => (
                        <th key={e} className="px-3 py-2 text-right font-semibold whitespace-nowrap">{e}</th>
                      ))}
                      <th className="px-3 py-2 text-right font-semibold">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dematByEntity.brokers.map(b => (
                      <tr key={b} style={{ borderTop: '1px solid var(--rule)' }}>
                        <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>{b}</td>
                        {dematByEntity.ents.map(e => {
                          const v = dematByEntity.cell(b, e);
                          return (
                            <td key={e} className="px-3 py-2 text-right" style={v === 0 ? { color: 'var(--ghost)' } : pnlStyle(v)}>
                              {v === 0 ? '—' : `₹${inr(v)}`}
                            </td>
                          );
                        })}
                        <td className="px-3 py-2 text-right font-medium" style={pnlStyle(dematByEntity.rowTotal(b))}>₹{inr(dematByEntity.rowTotal(b))}</td>
                      </tr>
                    ))}
                    <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                      <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>Total</td>
                      {dematByEntity.ents.map(e => (
                        <td key={e} className="px-3 py-2 text-right font-bold" style={pnlStyle(dematByEntity.colTotal(e))}>₹{inr(dematByEntity.colTotal(e))}</td>
                      ))}
                      <td className="px-3 py-2 text-right font-bold" style={pnlStyle(dematTotal)}>₹{inr(dematTotal)}</td>
                    </tr>
                  </tbody>
                </table>
                </div>
              </div>
            ) : dematBreak === 'yoy' ? (
              <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                      <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Demat / broker</th>
                      {dematByYoy.fys.map(fy => (
                        <th key={fy} className="px-3 py-2 text-right font-semibold whitespace-nowrap">FY {fy}</th>
                      ))}
                      <th className="px-3 py-2 text-right font-semibold">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dematByYoy.brokers.map(b => (
                      <tr key={b} style={{ borderTop: '1px solid var(--rule)' }}>
                        <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>{b}</td>
                        {dematByYoy.fys.map(fy => {
                          const v = dematByYoy.cell(b, fy);
                          return (
                            <td key={fy} className="px-3 py-2 text-right" style={v === 0 ? { color: 'var(--ghost)' } : pnlStyle(v)}>
                              {v === 0 ? '—' : `₹${inr(v)}`}
                            </td>
                          );
                        })}
                        <td className="px-3 py-2 text-right font-medium" style={pnlStyle(dematByYoy.rowTotal(b))}>₹{inr(dematByYoy.rowTotal(b))}</td>
                      </tr>
                    ))}
                    <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                      <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>Total</td>
                      {dematByYoy.fys.map(fy => (
                        <td key={fy} className="px-3 py-2 text-right font-bold" style={pnlStyle(dematByYoy.colTotal(fy))}>₹{inr(dematByYoy.colTotal(fy))}</td>
                      ))}
                      <td className="px-3 py-2 text-right font-bold" style={pnlStyle(dematTotal)}>₹{inr(dematTotal)}</td>
                    </tr>
                  </tbody>
                </table>
                </div>
              </div>
            ) : (
              // Entity × broker × year: (entity, broker) rows grouped under each entity
              // with a per-entity subtotal, FY columns.
              <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                      <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Entity</th>
                      <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Demat / broker</th>
                      {dematByEntityYoy.fys.map(fy => (
                        <th key={fy} className="px-3 py-2 text-right font-semibold whitespace-nowrap">FY {fy}</th>
                      ))}
                      <th className="px-3 py-2 text-right font-semibold">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dematByEntityYoy.entities.map(ent => {
                      const entRows = dematByEntityYoy.rowsFor(ent);
                      const entGrand = dematByEntityYoy.fys.reduce((s, fy) => s + dematByEntityYoy.entTotal(ent, fy), 0);
                      return (
                        <Fragment key={ent}>
                          {entRows.map((r, i) => (
                            <tr key={r.rk} style={{ borderTop: i === 0 ? '2px solid var(--rule)' : '1px solid var(--rule)' }}>
                              <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>
                                {i === 0 ? ent : ''}
                              </td>
                              <td className="px-3 py-2 whitespace-nowrap" style={{ color: 'var(--dim)' }}>{r.broker}</td>
                              {dematByEntityYoy.fys.map(fy => {
                                const v = dematByEntityYoy.cell(r.rk, fy);
                                return (
                                  <td key={fy} className="px-3 py-2 text-right" style={v === 0 ? { color: 'var(--ghost)' } : pnlStyle(v)}>
                                    {v === 0 ? '—' : `₹${inr(v)}`}
                                  </td>
                                );
                              })}
                              <td className="px-3 py-2 text-right font-medium" style={pnlStyle(dematByEntityYoy.rowTotal(r.rk))}>₹{inr(dematByEntityYoy.rowTotal(r.rk))}</td>
                            </tr>
                          ))}
                          {entRows.length > 1 && (
                            <tr style={{ background: 'var(--page)' }}>
                              <td className="px-3 py-1.5 text-xs font-semibold" style={{ color: 'var(--ghost)' }}>{ent} total</td>
                              <td />
                              {dematByEntityYoy.fys.map(fy => {
                                const v = dematByEntityYoy.entTotal(ent, fy);
                                return (
                                  <td key={fy} className="px-3 py-1.5 text-right font-semibold" style={v === 0 ? { color: 'var(--ghost)' } : pnlStyle(v)}>
                                    {v === 0 ? '—' : `₹${inr(v)}`}
                                  </td>
                                );
                              })}
                              <td className="px-3 py-1.5 text-right font-semibold" style={pnlStyle(entGrand)}>₹{inr(entGrand)}</td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                    <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                      <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>Total</td>
                      <td />
                      {dematByEntityYoy.fys.map(fy => (
                        <td key={fy} className="px-3 py-2 text-right font-bold" style={pnlStyle(dematByEntityYoy.colTotal(fy))}>₹{inr(dematByEntityYoy.colTotal(fy))}</td>
                      ))}
                      <td className="px-3 py-2 text-right font-bold" style={pnlStyle(dematByEntityYoy.grand)}>₹{inr(dematByEntityYoy.grand)}</td>
                    </tr>
                  </tbody>
                </table>
                </div>
              </div>
            )}

            {/* Broker-statement reconciliation (admin only). The statement's realised is
                the broker's own authority; a residual variance is what our FIFO still
                can't explain after the yfinance-validated corporate-action backfill. */}
            {recon && recon.rows.length > 0 && (
              <div className="mt-6">
                <h2 className="text-sm font-bold mb-1" style={{ color: 'var(--ink)' }}>
                  Reconciliation vs broker P&amp;L statements
                </h2>
                <p className="text-xs mb-3" style={{ color: 'var(--ghost)' }}>
                  The broker&apos;s own realised P&amp;L per demat × FY is the authority. A non-zero
                  variance is what our FIFO can&apos;t yet explain — a residual corporate action or a
                  missing/extra trade. F&amp;O is broker-only (we have no derivatives engine) and shown
                  as Derivatives.
                </p>
                <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
                  <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                        <th className="px-3 py-2 text-left font-semibold">Demat / broker</th>
                        <th className="px-3 py-2 text-left font-semibold">FY</th>
                        <th className="px-3 py-2 text-right font-semibold">Our FIFO</th>
                        <th className="px-3 py-2 text-right font-semibold">Broker statement</th>
                        <th className="px-3 py-2 text-right font-semibold">Variance</th>
                        <th className="px-3 py-2 text-right font-semibold">Derivatives (F&amp;O)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recon.rows.map(r => {
                        const matched = Math.abs(r.variance) <= Math.max(2000, Math.abs(r.stmtPnl) * 0.01);
                        return (
                          <tr key={`${r.broker}|${r.fy}`} style={{ borderTop: '1px solid var(--rule)' }}>
                            <td className="px-3 py-2 font-medium" style={{ color: 'var(--ink)' }}>{dematLabel(r.broker)}</td>
                            <td className="px-3 py-2" style={{ color: 'var(--dim)' }}>{r.fy}</td>
                            <td className="px-3 py-2 text-right" style={pnlStyle(r.ourPnl)}>₹{inr(r.ourPnl)}</td>
                            <td className="px-3 py-2 text-right font-semibold" style={pnlStyle(r.stmtPnl)}>₹{inr(r.stmtPnl)}</td>
                            <td className="px-3 py-2 text-right font-medium"
                                style={{ color: matched ? 'var(--gain)' : 'var(--peril)' }}>
                              {matched ? '✓ matched' : `₹${inr(r.variance)}`}
                            </td>
                            <td className="px-3 py-2 text-right" style={r.fnoPnl === 0 ? { color: 'var(--ghost)' } : pnlStyle(r.fnoPnl)}>
                              {r.fnoPnl === 0 ? '—' : `₹${inr(r.fnoPnl)}`}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  </div>
                </div>

                {/* Manage imported statements */}
                {stmts.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {stmts.map(s => (
                      <span key={s.id} className="inline-flex items-center gap-2 px-2 py-1 rounded-lg text-xs"
                            style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                        {s.entity_name} · {dematLabel(s.broker)} · {s.fy_label ?? '—'} · {s.n_lines} rows
                        <button
                          onClick={async () => {
                            if (!confirm(`Delete ${s.entity_name} ${s.broker} ${s.fy_label} statement?`)) return;
                            await fetch(`${API_URL}/api/v1/realised-gains/pnl-statement/${s.id}`,
                                        { method: 'DELETE', credentials: 'include' });
                            reloadStmts();
                          }}
                          style={{ color: 'var(--peril)' }}
                          title="Delete this statement"
                        >✕</button>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
          )
        ) : loading ? (
          <div className="py-16 text-center text-xs" style={{ color: 'var(--ghost)' }}>Loading…</div>
        ) : rows.length === 0 ? (
          <div className="py-16 text-center rounded-xl" style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}>
            <p className="text-sm font-medium mb-1" style={{ color: 'var(--dim)' }}>No realised gains this financial year</p>
            <p className="text-xs" style={{ color: 'var(--ghost)' }}>
              They appear as funds/stocks are sold (or after a tradebook import).
            </p>
          </div>
        ) : view === 'entity' ? (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  <th className="px-3 py-2 text-left font-semibold">Entity</th>
                  <th className="px-3 py-2 text-right font-semibold">Sales</th>
                  <th className="px-3 py-2 text-right font-semibold">Realised P&amp;L</th>
                  <th className="px-3 py-2 text-right font-semibold">Short-term</th>
                  <th className="px-3 py-2 text-right font-semibold">Long-term</th>
                  <th className="px-3 py-2 text-right font-semibold">Share</th>
                </tr>
              </thead>
              <tbody>
                {byEntity.map(([ent, v]) => (
                  <tr key={ent} style={{ borderTop: '1px solid var(--rule)' }}>
                    <td className="px-3 py-2 font-medium" style={{ color: 'var(--ink)' }}>{ent}</td>
                    <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>{v.n}</td>
                    <td className="px-3 py-2 text-right font-medium" style={pnlStyle(v.pnl)}>₹{inr(v.pnl)}</td>
                    <td className="px-3 py-2 text-right" style={pnlStyle(v.hasStLt ? v.st : null)}>
                      {v.hasStLt ? `₹${inr(v.st)}` : '—'}</td>
                    <td className="px-3 py-2 text-right" style={pnlStyle(v.hasStLt ? v.lt : null)}>
                      {v.hasStLt ? `₹${inr(v.lt)}` : '—'}</td>
                    {/* Share of the absolute total, so entities that lost money still
                        read as a positive contribution to the spread rather than a
                        negative percentage of a possibly-negative denominator. */}
                    <td className="px-3 py-2 text-right" style={{ color: 'var(--ghost)' }}>
                      {(() => {
                        const denom = byEntity.reduce((s, [, x]) => s + Math.abs(x.pnl), 0);
                        return denom ? `${((Math.abs(v.pnl) / denom) * 100).toFixed(1)}%` : '—';
                      })()}
                    </td>
                  </tr>
                ))}
                <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                  <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>Total</td>
                  <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--dim)' }}>{rows.length}</td>
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(totalPnl)}>₹{inr(totalPnl)}</td>
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(hasStLt ? totalSt : null)}>
                    {hasStLt ? `₹${inr(totalSt)}` : '—'}</td>
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(hasStLt ? totalLt : null)}>
                    {hasStLt ? `₹${inr(totalLt)}` : '—'}</td>
                  <td className="px-3 py-2 text-right font-bold" style={{ color: 'var(--dim)' }}>100%</td>
                </tr>
              </tbody>
            </table>
            </div>
          </div>
        ) : view === 'yoy' ? (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            {/* Entity columns can outgrow a narrow screen — scroll the table, never
                the page (see the responsive rule in the shared layout). */}
            <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  <th className="px-3 py-2 text-left font-semibold whitespace-nowrap">Financial year</th>
                  {yoy.ents.map(e => (
                    <th key={e} className="px-3 py-2 text-right font-semibold whitespace-nowrap">{e}</th>
                  ))}
                  <th className="px-3 py-2 text-right font-semibold">Total</th>
                </tr>
              </thead>
              <tbody>
                {yoy.fys.map(fy => (
                  <tr key={fy} style={{ borderTop: '1px solid var(--rule)' }}>
                    <td className="px-3 py-2 font-medium whitespace-nowrap" style={{ color: 'var(--ink)' }}>FY {fy}</td>
                    {yoy.ents.map(e => {
                      const v = yoy.cell(fy, e);
                      return (
                        <td key={e} className="px-3 py-2 text-right"
                            style={v === 0 ? { color: 'var(--ghost)' } : pnlStyle(v)}>
                          {v === 0 ? '—' : `₹${inr(v)}`}
                        </td>
                      );
                    })}
                    <td className="px-3 py-2 text-right font-medium" style={pnlStyle(yoy.rowTotal(fy))}>
                      ₹{inr(yoy.rowTotal(fy))}
                    </td>
                  </tr>
                ))}
                <tr style={{ borderTop: '2px solid var(--rule)', background: 'var(--page)' }}>
                  <td className="px-3 py-2 font-bold" style={{ color: 'var(--ink)' }}>All years</td>
                  {yoy.ents.map(e => (
                    <td key={e} className="px-3 py-2 text-right font-bold" style={pnlStyle(yoy.colTotal(e))}>
                      ₹{inr(yoy.colTotal(e))}
                    </td>
                  ))}
                  <td className="px-3 py-2 text-right font-bold" style={pnlStyle(yoy.grand)}>₹{inr(yoy.grand)}</td>
                </tr>
              </tbody>
            </table>
            </div>
          </div>
        ) : (
          <div style={{ background: 'var(--card)', border: '1px solid var(--rule)', borderRadius: 10, overflow: 'hidden' }}>
            <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr style={{ background: 'var(--page)', color: 'var(--dim)' }}>
                  {['Entity', 'Group', 'Security', 'Purchase Amt', 'Sale Date', 'Sale Amt', 'P&L', 'Short-term', 'Long-term', 'Return %'].map(h => (
                    <th key={h} className="px-3 py-2 text-left font-semibold">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sections.map(sec => {
                  const secPnl = sec.rows.reduce((s, r) => s + (r.pnl ?? 0), 0);
                  // ST/LT subtotals are meaningful only for sections that carry them
                  // (Indian equity). Elsewhere show a dash instead of a misleading ₹0.
                  const secHasStLt = sec.rows.some(r => r.st_pnl != null || r.lt_pnl != null);
                  const secSt = secHasStLt ? sec.rows.reduce((s, r) => s + (r.st_pnl ?? 0), 0) : null;
                  const secLt = secHasStLt ? sec.rows.reduce((s, r) => s + (r.lt_pnl ?? 0), 0) : null;
                  return (
                    <Fragment key={sec.cat}>
                      <tr style={{ background: 'var(--page)', borderTop: '1px solid var(--rule)' }}>
                        <td colSpan={6} className="px-3 py-1.5 font-semibold" style={{ color: 'var(--ink)' }}>
                          {sec.cat}
                          <span className="ml-2 font-normal" style={{ color: 'var(--ghost)' }}>({sec.rows.length})</span>
                        </td>
                        <td className="px-3 py-1.5 text-right font-semibold"
                            style={{ color: secPnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>₹{inr(secPnl)}</td>
                        <td className="px-3 py-1.5 text-right font-semibold"
                            style={{ color: secSt == null ? 'var(--ghost)' : secSt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                          {secSt == null ? '—' : `₹${inr(secSt)}`}</td>
                        <td className="px-3 py-1.5 text-right font-semibold"
                            style={{ color: secLt == null ? 'var(--ghost)' : secLt >= 0 ? 'var(--gain)' : 'var(--peril)' }}>
                          {secLt == null ? '—' : `₹${inr(secLt)}`}</td>
                        <td className="px-3 py-1.5" />
                      </tr>
                      {sec.rows.map((r, i) => (
                        <tr key={`${sec.cat}-${i}`} style={{ borderTop: '1px solid var(--rule)' }}>
                          <td className="px-3 py-2" style={{ color: 'var(--dim)' }}>{r.entity}</td>
                          <td className="px-3 py-2" style={{ color: 'var(--ghost)' }}>{r.group}</td>
                          <td className="px-3 py-2" style={{ color: 'var(--ink)' }}>{r.security_name}</td>
                          <td className="px-3 py-2 text-right">{inr(r.purchase_amount)}</td>
                          <td className="px-3 py-2">{r.sale_date}</td>
                          <td className="px-3 py-2 text-right">{inr(r.sale_amount)}</td>
                          <td className="px-3 py-2 text-right font-medium"
                              style={{ color: (r.pnl ?? 0) >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.pnl)}</td>
                          <td className="px-3 py-2 text-right"
                              style={{ color: r.st_pnl == null ? 'var(--ghost)' : r.st_pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.st_pnl)}</td>
                          <td className="px-3 py-2 text-right"
                              style={{ color: r.lt_pnl == null ? 'var(--ghost)' : r.lt_pnl >= 0 ? 'var(--gain)' : 'var(--peril)' }}>{inr(r.lt_pnl)}</td>
                          <td className="px-3 py-2 text-right">{pct(r.return_pct)}</td>
                        </tr>
                      ))}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

// ── Import modal ────────────────────────────────────────────────────────────────
// Admin uploads a Zerodha/Angel/Dhan realised-P&L statement. /preview parses +
// reconciles it against our FIFO (no DB write) so the discrepancies are shown before
// committing; /commit stores it as the per-scrip oracle. Entity is chosen here — the
// filename is only a hint and is never trusted (broker files are sometimes mislabelled).
interface ReconResult {
  broker: string; fy_label: string | null;
  stmt_total: number; our_total: number; variance: number;
  by_status: Record<string, { n: number; gap: number }>;
  scrips: { security_name: string; status: string; stmt_pnl: number; our_pnl: number | null; gap: number | null }[];
  fno_lines: { security_name: string; realised_pnl: number }[];
}
interface PreviewResp {
  entity_name: string; broker: string; client_id: string | null;
  fy_label: string | null; period_from: string | null; period_to: string | null;
  segment_totals: Record<string, Record<string, number>>;
  lines: { segment: string }[];
  reconciliation: ReconResult | null;
  committed: boolean;
}

const STATUS_LABEL: Record<string, string> = {
  MATCH: 'Matched', CA_COST_DRIFT: 'Corp-action cost drift',
  ISIN_MIGRATION: 'ISIN migration', SELL_GAP: 'Missing/extra trades', NO_DATA: 'No data our side',
};

function ImportPnlModal({ onClose, onCommitted }: { onClose: () => void; onCommitted: () => void }) {
  const API = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
  const [entities, setEntities] = useState<{ id: number; name: string }[]>([]);
  const [entityId, setEntityId] = useState<number | ''>('');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PreviewResp | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${API}/api/v1/entities`, { credentials: 'include' })
      .then(r => (r.ok ? r.json() : []))
      .then((j) => setEntities(Array.isArray(j) ? j : []))
      .catch(() => {});
  }, [API]);

  const send = async (commit: boolean) => {
    if (entityId === '' || !file) { setError('Pick an entity and a file.'); return; }
    setBusy(true); setError(null);
    try {
      const fd = new FormData();
      fd.append('entity_id', String(entityId));
      fd.append('file', file);
      const res = await fetch(
        `${API}/api/v1/realised-gains/pnl-statement/${commit ? 'commit' : 'preview'}`,
        { method: 'POST', credentials: 'include', body: fd });
      const j = await res.json();
      if (!res.ok) { setError(j?.detail || 'Upload failed.'); setBusy(false); return; }
      if (commit) { onCommitted(); return; }
      setPreview(j);
    } catch {
      setError('Network error.');
    }
    setBusy(false);
  };

  const rec = preview?.reconciliation ?? null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4"
         style={{ background: 'rgba(0,0,0,0.5)' }} onClick={onClose}>
      <div className="w-full max-w-3xl max-h-[88vh] overflow-y-auto rounded-xl p-5"
           style={{ background: 'var(--card)', border: '1px solid var(--rule)' }}
           onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-bold" style={{ color: 'var(--ink)' }}>Import broker P&amp;L statement</h2>
          <button onClick={onClose} style={{ color: 'var(--ghost)' }}>✕</button>
        </div>

        <div className="flex flex-wrap items-end gap-3 mb-4">
          <label className="text-xs" style={{ color: 'var(--ghost)' }}>
            <div className="mb-1">Entity</div>
            <select value={entityId} onChange={e => { setEntityId(e.target.value ? Number(e.target.value) : ''); setPreview(null); }}
                    className="px-2 py-1 rounded-lg text-xs"
                    style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--ink)' }}>
              <option value="">Select…</option>
              {entities.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
          </label>
          <label className="text-xs" style={{ color: 'var(--ghost)' }}>
            <div className="mb-1">Statement (Zerodha / Angel One / Dhan .xlsx or .csv)</div>
            <input type="file" accept=".xlsx,.csv"
                   onChange={e => { setFile(e.target.files?.[0] ?? null); setPreview(null); }}
                   className="text-xs" style={{ color: 'var(--dim)' }} />
          </label>
          <button onClick={() => send(false)} disabled={busy}
                  className="px-3 py-1 text-xs font-medium rounded-lg"
                  style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
            {busy ? 'Working…' : 'Preview'}
          </button>
        </div>

        {error && <div className="text-xs mb-3" style={{ color: 'var(--peril)' }}>{error}</div>}

        {preview && (
          <>
            <div className="text-xs mb-3" style={{ color: 'var(--dim)' }}>
              <strong>{preview.entity_name}</strong> · {dematLabel(preview.broker)} · client {preview.client_id ?? '—'} ·{' '}
              {preview.period_from} → {preview.period_to} · {preview.fy_label ?? '—'} ·{' '}
              {preview.lines.length} scrip rows
            </div>

            {rec && (
              <>
                <div className="flex flex-wrap gap-4 text-xs mb-3">
                  <span style={{ color: 'var(--ghost)' }}>Broker statement: <strong style={pnlStyle(rec.stmt_total)}>₹{inr(rec.stmt_total)}</strong></span>
                  <span style={{ color: 'var(--ghost)' }}>Our FIFO: <strong style={pnlStyle(rec.our_total)}>₹{inr(rec.our_total)}</strong></span>
                  <span style={{ color: 'var(--ghost)' }}>Variance: <strong style={{ color: Math.abs(rec.variance) <= 2000 ? 'var(--gain)' : 'var(--peril)' }}>₹{inr(rec.variance)}</strong></span>
                </div>
                <div className="flex flex-wrap gap-2 mb-3">
                  {Object.entries(rec.by_status).map(([st, v]) => (
                    <span key={st} className="px-2 py-1 rounded-lg text-xs"
                          style={{ background: 'var(--page)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>
                      {STATUS_LABEL[st] ?? st}: {v.n}{Math.abs(v.gap) > 1 ? ` (₹${inr(v.gap)})` : ''}
                    </span>
                  ))}
                </div>
                {/* Scrips that don't match, worst first — the ones a backfill would target. */}
                {rec.scrips.filter(s => s.status !== 'MATCH').length > 0 && (
                  <div className="overflow-x-auto mb-3" style={{ maxHeight: 220 }}>
                    <table className="w-full text-xs">
                      <thead><tr style={{ color: 'var(--ghost)' }}>
                        <th className="px-2 py-1 text-left">Scrip</th><th className="px-2 py-1 text-left">Status</th>
                        <th className="px-2 py-1 text-right">Broker</th><th className="px-2 py-1 text-right">Ours</th>
                        <th className="px-2 py-1 text-right">Gap</th>
                      </tr></thead>
                      <tbody>
                        {rec.scrips.filter(s => s.status !== 'MATCH')
                          .sort((a, b) => Math.abs(b.gap ?? 0) - Math.abs(a.gap ?? 0))
                          .map((s, i) => (
                          <tr key={i} style={{ borderTop: '1px solid var(--rule)' }}>
                            <td className="px-2 py-1" style={{ color: 'var(--ink)' }}>{s.security_name}</td>
                            <td className="px-2 py-1" style={{ color: 'var(--dim)' }}>{STATUS_LABEL[s.status] ?? s.status}</td>
                            <td className="px-2 py-1 text-right" style={pnlStyle(s.stmt_pnl)}>₹{inr(s.stmt_pnl)}</td>
                            <td className="px-2 py-1 text-right" style={pnlStyle(s.our_pnl)}>₹{inr(s.our_pnl)}</td>
                            <td className="px-2 py-1 text-right" style={pnlStyle(s.gap)}>₹{inr(s.gap)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {rec.fno_lines.length > 0 && (
                  <div className="text-xs mb-3" style={{ color: 'var(--ghost)' }}>
                    Derivatives (F&amp;O), broker-only: {rec.fno_lines.length} rows, realised ₹
                    {inr(rec.fno_lines.reduce((s, l) => s + l.realised_pnl, 0))}.
                  </div>
                )}
              </>
            )}

            <div className="flex justify-end gap-2">
              <button onClick={onClose} className="px-3 py-1 text-xs rounded-lg"
                      style={{ background: 'var(--card)', border: '1px solid var(--rule)', color: 'var(--dim)' }}>Cancel</button>
              <button onClick={() => send(true)} disabled={busy}
                      className="px-3 py-1 text-xs font-medium rounded-lg" style={{ background: 'var(--prime)', color: '#fff' }}>
                {busy ? 'Importing…' : 'Confirm import'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
