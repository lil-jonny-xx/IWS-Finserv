'use client';
import { useEffect, useState, useCallback, useRef } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

const CURRENCIES     = ['INR', 'USD', 'GBP', 'EUR', 'AED', 'SGD', 'HKD'];
const ACCOUNT_TYPES  = ['savings', 'current', 'nre', 'other'];

interface User   { role: string; full_name: string; entity_id?: number; }
interface Entity { id: number; entity_name?: string; name?: string; }

interface BankAccount {
  id: number;
  entity_id: number;
  entity_name: string;
  bank_name: string;
  account_type: string;
  currency: string;
  balance: number;
  balance_inr: number | null;
  fx_rate: number | null;
  balance_as_of: string | null;
  notes: string | null;
  statement_count: number;
  updated_at: string | null;
  updated_by: string | null;
}

interface BankAccountsResponse {
  accounts: BankAccount[];
  total_inr: number;
  fx_rates: Record<string, number>;
}

interface ParsedStatement {
  statement_id: number;
  parsed_balance: number | null;
  parsed_as_of: string | null;
  parse_status: 'parsed' | 'needs_review' | 'failed';
  parse_note: string;
  file_kind: string;
}

const NAV = [
  { href: '/dashboard',      label: 'Overview'       },
  { href: '/mutual-funds',   label: 'Mutual Funds'   },
  { href: '/equity',         label: 'Equity'         },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/gold-silver', label: 'Gold/Silver' },
  { href: '/bank-accounts',  label: 'Banks', active: true },
  { href: '/pms',            label: 'PMS'            },
  { href: '/manual-data',    label: 'Manual Data'    },
  { href: '/reports',        label: 'Reports'        },
  { href: '/benchmarks',     label: 'Benchmarks'     },
  { href: '/realised-gains', label: 'Realised Gains' },
  { href: '/assistant',      label: 'Assistant'      },
];

function entityName(e: Entity): string { return e.entity_name || e.name || `#${e.id}`; }

function fmtINR(n: number | null): string {
  if (n === null || n === undefined) return '—';
  return new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(n);
}
function fmtNative(n: number, ccy: string): string {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: ccy, maximumFractionDigits: 2 }).format(n);
}
function fmtAsOf(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
}

// ---------------------------------------------------------------------------

function EntitySwitcher({ entities, selectedId, onSelect }: {
  entities: Entity[]; selectedId: number | null; onSelect: (id: number | null) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5 mb-5" role="tablist" aria-label="Entity filter">
      {[{ id: null, name: 'All' }, ...entities.map(e => ({ id: e.id, name: entityName(e) }))].map(tab => {
        const active = tab.id === selectedId;
        return (
          <button key={tab.id ?? 'all'} role="tab" aria-selected={active}
            onClick={() => onSelect(tab.id ?? null)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              active ? 'bg-prime text-prime-fg'
                     : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
            {tab.name}
          </button>
        );
      })}
    </div>
  );
}

function AddAccountForm({ entities, onCreated }: { entities: Entity[]; onCreated: () => void }) {
  const [open, setOpen]       = useState(false);
  const [entityId, setEntity] = useState<number | ''>('');
  const [bankName, setBank]   = useState('');
  const [type, setType]       = useState('savings');
  const [currency, setCcy]    = useState('INR');
  const [saving, setSaving]   = useState(false);
  const [error, setError]     = useState<string | null>(null);

  async function submit() {
    if (!entityId || !bankName.trim()) { setError('Entity and bank name are required.'); return; }
    setSaving(true); setError(null);
    try {
      const r = await fetch(`${API_URL}/api/v1/bank-accounts`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: entityId, bank_name: bankName.trim(), account_type: type, currency }),
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'Failed to create account.'); }
      setBank(''); setEntity(''); setType('savings'); setCcy('INR'); setOpen(false);
      onCreated();
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to create account.'); }
    finally { setSaving(false); }
  }

  if (!open) {
    return (
      <button onClick={() => setOpen(true)}
        className="mb-5 text-xs font-medium bg-prime text-prime-fg px-3 py-1.5 rounded hover:opacity-90 transition-opacity">
        + Add bank account
      </button>
    );
  }
  return (
    <div className="mb-5 bg-card rounded-lg border border-rule p-4">
      <div className="flex flex-wrap items-end gap-3">
        <label className="text-xs text-dim flex flex-col gap-1">Entity
          <select value={entityId} onChange={e => setEntity(e.target.value ? Number(e.target.value) : '')}
            className="bg-page border border-rule rounded px-2 py-1.5 text-sm text-ink min-w-32">
            <option value="">Select…</option>
            {entities.map(e => <option key={e.id} value={e.id}>{entityName(e)}</option>)}
          </select>
        </label>
        <label className="text-xs text-dim flex flex-col gap-1">Bank name
          <input value={bankName} onChange={e => setBank(e.target.value)} placeholder="e.g. UK HSBC"
            className="bg-page border border-rule rounded px-2 py-1.5 text-sm text-ink min-w-48" />
        </label>
        <label className="text-xs text-dim flex flex-col gap-1">Type
          <select value={type} onChange={e => setType(e.target.value)}
            className="bg-page border border-rule rounded px-2 py-1.5 text-sm text-ink capitalize">
            {ACCOUNT_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <label className="text-xs text-dim flex flex-col gap-1">Currency
          <select value={currency} onChange={e => setCcy(e.target.value)}
            className="bg-page border border-rule rounded px-2 py-1.5 text-sm text-ink">
            {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        </label>
        <button onClick={submit} disabled={saving}
          className="text-xs font-medium bg-prime text-prime-fg px-3 py-2 rounded hover:opacity-90 disabled:opacity-50">
          {saving ? 'Saving…' : 'Create'}
        </button>
        <button onClick={() => { setOpen(false); setError(null); }}
          className="text-xs border border-rule text-dim px-3 py-2 rounded hover:text-ink">Cancel</button>
      </div>
      {error && <p className="text-xs text-red-500 mt-2">{error}</p>}
    </div>
  );
}

function AccountCard({ acct, onChanged }: { acct: BankAccount; onChanged: () => void }) {
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy]       = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [parsed, setParsed]   = useState<ParsedStatement | null>(null);
  const [manual, setManual]   = useState(false);
  // confirm/manual entry fields
  const [bal, setBal]   = useState('');
  const [asOf, setAsOf] = useState('');

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    setBusy(true); setError(null); setParsed(null); setManual(false);
    try {
      const fd = new FormData();
      fd.append('file', f);
      const r = await fetch(`${API_URL}/api/v1/bank-accounts/${acct.id}/statements`, {
        method: 'POST', credentials: 'include', body: fd,   // browser sets multipart boundary
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'Upload failed.'); }
      const p: ParsedStatement = await r.json();
      setParsed(p);
      setBal(p.parsed_balance !== null ? String(p.parsed_balance) : '');
      setAsOf(p.parsed_as_of || '');
    } catch (e) { setError(e instanceof Error ? e.message : 'Upload failed.'); }
    finally { setBusy(false); if (fileRef.current) fileRef.current.value = ''; }
  }

  async function commit(statementId: number | null) {
    if (bal === '' || isNaN(Number(bal))) { setError('Enter a valid balance.'); return; }
    setBusy(true); setError(null);
    try {
      const r = await fetch(`${API_URL}/api/v1/bank-accounts/${acct.id}/balance`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ balance: Number(bal), balance_as_of: asOf || null, statement_id: statementId }),
      });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'Failed to save balance.'); }
      setParsed(null); setManual(false); onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to save balance.'); }
    finally { setBusy(false); }
  }

  async function remove() {
    if (!confirm(`Delete ${acct.bank_name} (${acct.entity_name})? This removes its statement history.`)) return;
    setBusy(true); setError(null);
    try {
      const r = await fetch(`${API_URL}/api/v1/bank-accounts/${acct.id}/delete`, { method: 'POST', credentials: 'include' });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.detail || 'Failed to delete.'); }
      onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : 'Failed to delete.'); setBusy(false); }
  }

  const statusColor = parsed?.parse_status === 'parsed' ? 'text-emerald-500'
                    : parsed?.parse_status === 'needs_review' ? 'text-amber-500' : 'text-red-500';

  return (
    <div className="bg-card rounded-lg border border-rule p-5 flex flex-col gap-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-ink">{acct.bank_name}</h3>
          <div className="flex items-center gap-2 mt-0.5">
            <span className="text-xs text-dim">{acct.entity_name}</span>
            <span className="text-[10px] uppercase tracking-wide bg-page border border-rule text-ghost px-1.5 py-0.5 rounded">
              {acct.account_type}
            </span>
          </div>
        </div>
        <button onClick={remove} disabled={busy} title="Delete account"
          className="text-ghost hover:text-red-500 text-xs px-1.5 py-0.5 rounded disabled:opacity-50">✕</button>
      </div>

      <div>
        <div className="text-2xl font-bold text-ink tabular-nums">{fmtNative(acct.balance, acct.currency)}</div>
        <div className="text-xs text-ghost mt-0.5">
          {acct.balance_inr !== null
            ? <>≈ {fmtINR(acct.balance_inr)}{acct.fx_rate ? ` · 1 ${acct.currency} = ₹${acct.fx_rate.toFixed(2)}` : ''}</>
            : <span className="text-amber-500">No FX rate for {acct.currency} yet</span>}
          {acct.balance_as_of && <> · as of {fmtAsOf(acct.balance_as_of)}</>}
        </div>
      </div>

      {/* upload + confirm */}
      <div className="border-t border-rule pt-3">
        {!parsed && !manual && (
          <div className="flex flex-wrap items-center gap-2">
            <input ref={fileRef} type="file" accept=".pdf,.csv,.xlsx,.xls" onChange={onFile} disabled={busy}
              className="text-xs text-dim file:mr-2 file:text-xs file:font-medium file:border-0 file:rounded
                         file:bg-prime file:text-prime-fg file:px-2.5 file:py-1.5 file:cursor-pointer" />
            <button onClick={() => { setManual(true); setBal(String(acct.balance)); setAsOf(acct.balance_as_of || ''); }}
              className="text-xs text-dim hover:text-ink underline underline-offset-2">enter manually</button>
            {acct.statement_count > 0 && (
              <span className="text-[11px] text-ghost ml-auto">{acct.statement_count} statement{acct.statement_count > 1 ? 's' : ''} uploaded</span>
            )}
          </div>
        )}

        {busy && !parsed && <p className="text-xs text-ghost">Working…</p>}

        {(parsed || manual) && (
          <div className="bg-page rounded-md border border-rule p-3 flex flex-col gap-2">
            {parsed && (
              <p className={`text-xs ${statusColor}`}>
                {parsed.parse_status === 'parsed' ? 'Parsed from statement.' :
                 parsed.parse_status === 'needs_review' ? 'Please review:' : 'Could not parse:'} {' '}
                <span className="text-dim">{parsed.parse_note}</span>
              </p>
            )}
            <div className="flex flex-wrap items-end gap-3">
              <label className="text-xs text-dim flex flex-col gap-1">Balance ({acct.currency})
                <input value={bal} onChange={e => setBal(e.target.value)} inputMode="decimal"
                  className="bg-card border border-rule rounded px-2 py-1.5 text-sm text-ink w-40 tabular-nums" />
              </label>
              <label className="text-xs text-dim flex flex-col gap-1">As of
                <input type="date" value={asOf} onChange={e => setAsOf(e.target.value)}
                  className="bg-card border border-rule rounded px-2 py-1.5 text-sm text-ink" />
              </label>
              <button onClick={() => commit(parsed ? parsed.statement_id : null)} disabled={busy}
                className="text-xs font-medium bg-prime text-prime-fg px-3 py-2 rounded hover:opacity-90 disabled:opacity-50">
                {busy ? 'Saving…' : 'Confirm & save'}
              </button>
              <button onClick={() => { setParsed(null); setManual(false); setError(null); }}
                className="text-xs border border-rule text-dim px-3 py-2 rounded hover:text-ink">Cancel</button>
            </div>
          </div>
        )}

        {error && <p className="text-xs text-red-500 mt-2">{error}</p>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

export default function BankAccountsPage() {
  const router = useRouter();
  const [user, setUser]         = useState<User | null>(null);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [selectedId, setSel]    = useState<number | null>(null);
  const [data, setData]         = useState<BankAccountsResponse | null>(null);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState<string | null>(null);
  const [reload, setReload]     = useState(0);
  const refresh = useCallback(() => setReload(c => c + 1), []);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        if (u.role === 'admin') {
          fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
            .then(r => r.ok ? r.json() : []).then((ents: Entity[]) => setEntities(ents)).catch(() => {});
        } else {
          setLoading(false);   // non-admins see the access notice, not a spinner
        }
      })
      .catch(() => router.push('/'));
  }, [router]);

  useEffect(() => {
    if (!user || user.role !== 'admin') return;
    const controller = new AbortController();
    setError(null);
    const qs = selectedId !== null ? `?entity_id=${selectedId}` : '';
    fetch(`${API_URL}/api/v1/bank-accounts${qs}`, { credentials: 'include', signal: controller.signal })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } if (!r.ok) throw new Error('Failed to load bank accounts.'); return r.json(); })
      .then((d: BankAccountsResponse | null) => { if (d) setData(d); setLoading(false); })
      .catch(err => { if (err.name !== 'AbortError') { setError(err.message); setLoading(false); } });
    return () => controller.abort();
  }, [router, user, selectedId, reload]);

  const isAdmin = user?.role === 'admin';

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Bank Accounts</h1>
            <p className="text-sm text-ghost mt-0.5">Cash balances by entity · upload a statement (PDF / CSV / Excel) to update</p>
          </div>
          <nav className="flex flex-wrap gap-1.5" aria-label="Sections">
            {NAV.map(({ href, label, active }) => (
              <a key={href} href={href} aria-current={active ? 'page' : undefined}
                className={`px-3 py-1.5 rounded text-xs font-medium transition-colors ${
                  active ? 'bg-prime text-prime-fg'
                         : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
                {label}
              </a>
            ))}
          </nav>
        </div>

        {isAdmin === false && (
          <div className="bg-card rounded-lg border border-rule px-5 py-8 text-center">
            <p className="text-sm text-dim">Bank accounts are managed by administrators.</p>
          </div>
        )}

        {isAdmin && (
          <>
            {entities.length > 0 && <EntitySwitcher entities={entities} selectedId={selectedId} onSelect={setSel} />}
            <AddAccountForm entities={entities} onCreated={refresh} />

            {data && (
              <div className="mb-5 bg-card rounded-lg border border-rule px-5 py-4 inline-flex flex-col">
                <span className="text-xs text-ghost uppercase tracking-wide">Total (INR equiv.)</span>
                <span className="text-xl font-bold text-ink tabular-nums">{fmtINR(data.total_inr)}</span>
              </div>
            )}

            {loading && !data && <p className="text-sm text-ghost">Loading…</p>}
            {error && <p className="text-sm text-red-500 mb-4">{error}</p>}

            {data && data.accounts.length === 0 && (
              <div className="bg-card rounded-lg border border-rule px-5 py-8 text-center">
                <p className="text-sm text-dim">No bank accounts yet. Add one above.</p>
              </div>
            )}

            {data && data.accounts.length > 0 && (
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 fade-in">
                {data.accounts.map(a => <AccountCard key={a.id} acct={a} onChanged={refresh} />)}
              </div>
            )}
          </>
        )}

        <p className="text-center text-xs text-ghost mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Bank balances are entered from uploaded statements
        </p>
      </div>
    </main>
  );
}
