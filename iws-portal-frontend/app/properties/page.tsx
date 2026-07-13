'use client';
import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import NavTabs from '@/app/components/NavTabs';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface User { role: string; full_name: string; entity_id?: number; }
interface Holder { id: number; name: string; short_code: string | null; grp: 'main' | 'parent'; is_custom: boolean; }
interface DocType { slug: string; label: string; scope: 'land' | 'building'; optional: boolean; parent: string | null; }
interface PropDoc {
  id: number; doc_type: string; original_name: string | null; mime: string | null;
  size_bytes: number | null; converted: boolean; has_original: boolean; uploaded_at: string | null;
}
interface Property {
  id: number; name: string; property_type: 'land' | 'building';
  holder_id: number; holder_name: string;
  location: string | null; taluka: string | null;
  area: number | null; area_unit: string | null; deed_no: string | null;
  acquisition_date: string | null; ownership: string | null;
  rrr: number | null; fair_value: number | null; notes: string | null;
  documents: PropDoc[]; missing_required: string[];
}
interface PropertiesResponse { count: number; total_fair_value: number; properties: Property[]; }

interface PropertyForm {
  id: number | null; name: string; property_type: 'land' | 'building'; holder_id: number | '';
  location: string; taluka: string; area: string; area_unit: string; deed_no: string;
  acquisition_date: string; ownership: string; rrr: string; notes: string;
}
const EMPTY_FORM: PropertyForm = {
  id: null, name: '', property_type: 'land', holder_id: '', location: '', taluka: '',
  area: '', area_unit: 'sq m', deed_no: '', acquisition_date: '', ownership: '', rrr: '', notes: '',
};

function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}
function docUrl(id: number, original = false) {
  return `${API_URL}/api/v1/property-documents/${id}/file${original ? '?original=true' : ''}`;
}
function holderLabel(h: Holder) { return h.short_code || h.name; }

export default function PropertiesPage() {
  const router = useRouter();
  const [user, setUser]         = useState<User | null>(null);
  const [holders, setHolders]   = useState<Holder[]>([]);
  const [docTypes, setDocTypes] = useState<DocType[]>([]);
  const [fairMult, setFairMult] = useState(1.75);
  const [data, setData]         = useState<PropertiesResponse | null>(null);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState<string | null>(null);
  const [busy, setBusy]         = useState<string | null>(null);

  // Holder tabs: null = All; 'parent' = the Parent Companies tab (with sub-tabs).
  const [tab, setTab]           = useState<number | 'parent' | null>(null);
  const [parentSub, setParentSub] = useState<number | null>(null);

  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [form, setForm]         = useState<PropertyForm | null>(null);
  const [formErr, setFormErr]   = useState<string | null>(null);
  const [newEntity, setNewEntity] = useState('');
  const [uploadSel, setUploadSel] = useState<Record<number, string>>({}); // propId -> doc slug
  const fileRef                 = useRef<HTMLInputElement | null>(null);
  const pendingUpload           = useRef<{ propId: number; slug: string } | null>(null);

  const isAdmin = user?.role === 'admin';

  const loadStatic = useCallback(() => {
    fetch(`${API_URL}/api/v1/property-entities`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : []).then(setHolders).catch(() => {});
    fetch(`${API_URL}/api/v1/property-doc-types`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) { setDocTypes(d.doc_types); setFairMult(d.fair_value_multiplier); } })
      .catch(() => {});
  }, []);

  const loadProperties = useCallback(() => {
    setError(null);
    fetch(`${API_URL}/api/v1/properties`, { credentials: 'include' })
      .then(r => {
        if (r.status === 401) { router.push('/'); return null; }
        if (!r.ok) throw new Error('Failed to load properties.');
        return r.json();
      })
      .then((d: PropertiesResponse | null) => { if (d) setData(d); setLoading(false); })
      .catch(err => { setError(err.message); setLoading(false); });
  }, [router]);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => { if (u) { setUser(u); loadStatic(); loadProperties(); } })
      .catch(() => router.push('/'));
  }, [router, loadStatic, loadProperties]);

  const mainHolders   = useMemo(() => holders.filter(h => h.grp === 'main'), [holders]);
  const parentHolders = useMemo(() => holders.filter(h => h.grp === 'parent'), [holders]);
  const parentIds     = useMemo(() => new Set(parentHolders.map(h => h.id)), [parentHolders]);

  const visible = useMemo(() => {
    const all = data?.properties ?? [];
    if (tab === null) return all;
    if (tab === 'parent') {
      return parentSub === null ? all.filter(p => parentIds.has(p.holder_id))
                                : all.filter(p => p.holder_id === parentSub);
    }
    return all.filter(p => p.holder_id === tab);
  }, [data, tab, parentSub, parentIds]);

  const visibleTotal = useMemo(
    () => visible.reduce((s, p) => s + (p.fair_value ?? 0), 0), [visible]);

  const docTypesFor = useCallback((type: 'land' | 'building') =>
    type === 'building' ? docTypes : docTypes.filter(d => d.scope === 'land'), [docTypes]);

  // ---- actions ------------------------------------------------------------

  const saveEntity = () => {
    const name = newEntity.trim();
    if (!name) return;
    setBusy('entity');
    fetch(`${API_URL}/api/v1/property-entities`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, grp: 'parent' }),
    })
      .then(r => { if (!r.ok) throw new Error('Could not add entity'); return r.json(); })
      .then(() => { setNewEntity(''); loadStatic(); })
      .catch(e => alert(e.message))
      .finally(() => setBusy(null));
  };

  const saveProperty = () => {
    if (!form) return;
    if (!form.name.trim() || form.holder_id === '') { setFormErr('Name and entity are required.'); return; }
    setFormErr(null); setBusy('save');
    const body = {
      name: form.name.trim(), property_type: form.property_type, holder_id: form.holder_id,
      location: form.location || null, taluka: form.taluka || null,
      area: form.area ? Number(form.area) : null, area_unit: form.area_unit || 'sq m',
      deed_no: form.deed_no || null, acquisition_date: form.acquisition_date || null,
      ownership: form.ownership || null, rrr: form.rrr ? Number(form.rrr) : null,
      notes: form.notes || null,
    };
    fetch(`${API_URL}/api/v1/properties${form.id != null ? `/${form.id}` : ''}`, {
      method: form.id != null ? 'PUT' : 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
      .then(r => { if (!r.ok) return r.json().then((e: { detail?: string }) => { throw new Error(e.detail || 'Save failed'); }); return r.json(); })
      .then(() => { setForm(null); loadProperties(); })
      .catch(e => setFormErr(e.message))
      .finally(() => setBusy(null));
  };

  const deleteProperty = (p: Property) => {
    if (!window.confirm(`Delete "${p.name}" and all its documents?`)) return;
    fetch(`${API_URL}/api/v1/properties/${p.id}`, { method: 'DELETE', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Delete failed'); loadProperties(); })
      .catch(e => alert(e.message));
  };

  const startUpload = (propId: number, slug: string) => {
    if (!slug) { alert('Choose a document type first.'); return; }
    pendingUpload.current = { propId, slug };
    fileRef.current?.click();
  };

  const onFileChosen = (files: FileList | null) => {
    const target = pendingUpload.current;
    pendingUpload.current = null;
    if (!files || files.length === 0 || !target) return;
    const fd = new FormData();
    fd.append('doc_type', target.slug);
    fd.append('file', files[0]);
    setBusy(`upload-${target.propId}`);
    fetch(`${API_URL}/api/v1/properties/${target.propId}/documents`, {
      method: 'POST', credentials: 'include', body: fd,
    })
      .then(r => { if (!r.ok) return r.json().then((e: { detail?: string }) => { throw new Error(e.detail || 'Upload failed'); }); return r.json(); })
      .then(() => loadProperties())
      .catch(e => alert(e.message))
      .finally(() => { setBusy(null); if (fileRef.current) fileRef.current.value = ''; });
  };

  const deleteDoc = (d: PropDoc) => {
    if (!window.confirm(`Delete ${d.original_name || 'this document'}?`)) return;
    fetch(`${API_URL}/api/v1/property-documents/${d.id}`, { method: 'DELETE', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Delete failed'); loadProperties(); })
      .catch(e => alert(e.message));
  };

  const toggleExpand = (id: number) =>
    setExpanded(prev => { const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n; });

  // ---- render -------------------------------------------------------------

  const tabClass = (active: boolean) =>
    `px-3 py-1 rounded text-xs font-medium transition-colors ${
      active ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`;

  const editForm = (p: Property) => setForm({
    id: p.id, name: p.name, property_type: p.property_type, holder_id: p.holder_id,
    location: p.location ?? '', taluka: p.taluka ?? '',
    area: p.area != null ? String(p.area) : '', area_unit: p.area_unit ?? 'sq m',
    deed_no: p.deed_no ?? '', acquisition_date: p.acquisition_date ?? '',
    ownership: p.ownership ?? '', rrr: p.rrr != null ? String(p.rrr) : '', notes: p.notes ?? '',
  });

  const formFair = form && form.area && form.rrr
    ? Number(form.area) * Number(form.rrr) * fairMult : null;

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Properties</h1>
            <span className="text-sm text-ghost">Land & building register — documents, circle rates and fair values</span>
          </div>
          <NavTabs active="/properties" role={user?.role} />
        </div>

        {/* Holder tabs (property-specific — not the portal entity switcher) */}
        <div className="flex flex-wrap gap-1.5 mb-2" role="tablist" aria-label="Holding entity">
          <button role="tab" aria-selected={tab === null} className={tabClass(tab === null)}
                  onClick={() => setTab(null)}>All</button>
          {mainHolders.map(h => (
            <button key={h.id} role="tab" aria-selected={tab === h.id} title={h.name}
                    className={tabClass(tab === h.id)} onClick={() => setTab(h.id)}>
              {holderLabel(h)}
            </button>
          ))}
          <button role="tab" aria-selected={tab === 'parent'} className={tabClass(tab === 'parent')}
                  onClick={() => { setTab('parent'); setParentSub(null); }}>Parent Companies</button>
        </div>

        {tab === 'parent' && (
          <div className="flex flex-wrap items-center gap-1.5 mb-3 pl-3 border-l-2 border-rule"
               role="tablist" aria-label="Parent company">
            <button role="tab" aria-selected={parentSub === null} className={tabClass(parentSub === null)}
                    onClick={() => setParentSub(null)}>All</button>
            {parentHolders.map(h => (
              <button key={h.id} role="tab" aria-selected={parentSub === h.id} title={h.name}
                      className={tabClass(parentSub === h.id)} onClick={() => setParentSub(h.id)}>
                {holderLabel(h)}
              </button>
            ))}
            {isAdmin && (
              <span className="flex items-center gap-1.5 ml-2">
                <input value={newEntity} onChange={e => setNewEntity(e.target.value)}
                       onKeyDown={e => { if (e.key === 'Enter') saveEntity(); }}
                       placeholder="Other entity…"
                       className="text-xs bg-card border border-rule rounded px-2 py-1 text-ink w-36" />
                <button onClick={saveEntity} disabled={busy === 'entity' || !newEntity.trim()}
                        className="text-xs border border-wire text-dim px-2 py-1 rounded hover:border-dim hover:text-ink transition-colors disabled:opacity-50">
                  + Add
                </button>
              </span>
            )}
          </div>
        )}

        {loading && !data && (
          <div className="bg-card rounded-lg border border-rule px-5 py-16 text-center text-sm text-ghost">Loading…</div>
        )}
        {error && !data && (
          <div role="alert" className="bg-card rounded-lg border border-rule px-5 py-5 flex items-start justify-between gap-4">
            <div>
              <p className="text-sm font-medium text-dim">Could not load properties</p>
              <p className="text-xs text-ghost mt-1">{error}</p>
            </div>
            <button onClick={loadProperties} className="shrink-0 text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Retry</button>
          </div>
        )}

        {data && (
          <div className="fade-in">
            <div className="bg-card rounded-lg border border-rule px-5 sm:px-6 py-4 mb-4 flex flex-wrap gap-8 items-end">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Fair Value ({fairMult}× RRR)</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(visibleTotal)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Properties</p>
                <p className="text-base font-semibold text-ink tabular-nums">{visible.length}</p>
              </div>
              {isAdmin && (
                <button onClick={() => { setFormErr(null); setForm({ ...EMPTY_FORM }); }}
                        className="ml-auto text-xs bg-prime text-prime-fg px-3 py-1.5 rounded font-medium hover:opacity-90 transition-opacity">
                  + Add Property
                </button>
              )}
            </div>

            <div className="bg-card rounded-lg border border-rule overflow-x-auto">
              <table className="w-full text-xs min-w-[1100px]">
                <thead>
                  <tr className="border-b border-rule text-left text-[11px] uppercase tracking-wide text-ghost">
                    <th className="px-3 py-2.5">Name</th>
                    <th className="px-3 py-2.5">Entity</th>
                    <th className="px-3 py-2.5">Location</th>
                    <th className="px-3 py-2.5">Taluka</th>
                    <th className="px-3 py-2.5 text-right">Area</th>
                    <th className="px-3 py-2.5">Deed</th>
                    <th className="px-3 py-2.5">Acquired</th>
                    <th className="px-3 py-2.5">Ownership</th>
                    <th className="px-3 py-2.5 text-right">RRR</th>
                    <th className="px-3 py-2.5 text-right">Fair Value</th>
                    <th className="px-3 py-2.5">Valuation Report</th>
                    <th className="px-3 py-2.5">Documents</th>
                    {isAdmin && <th className="px-3 py-2.5" />}
                  </tr>
                </thead>
                <tbody>
                  {visible.length === 0 && (
                    <tr><td colSpan={isAdmin ? 13 : 12} className="px-3 py-12 text-center text-ghost">
                      No properties recorded{tab !== null ? ' for this entity' : ''} yet.
                    </td></tr>
                  )}
                  {visible.map(p => {
                    const types      = docTypesFor(p.property_type);
                    const requiredN  = types.filter(t => !t.optional).length;
                    const missingN   = p.missing_required.length;
                    const valReports = p.documents.filter(d => d.doc_type === 'valuation_report');
                    const isOpen     = expanded.has(p.id);
                    return (
                      <PropertyRows key={p.id} p={p} types={types} isOpen={isOpen} isAdmin={isAdmin}
                        requiredN={requiredN} missingN={missingN} valReports={valReports}
                        uploadSel={uploadSel[p.id] ?? ''} busy={busy === `upload-${p.id}`}
                        onToggle={() => toggleExpand(p.id)}
                        onEdit={() => { setFormErr(null); editForm(p); }}
                        onDelete={() => deleteProperty(p)}
                        onSelectDoc={slug => setUploadSel(s => ({ ...s, [p.id]: slug }))}
                        onUpload={slug => startUpload(p.id, slug)}
                        onDeleteDoc={deleteDoc}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">IWS Finserv &copy; {new Date().getFullYear()}</p>
      </div>

      <input ref={fileRef} type="file" className="hidden" aria-hidden="true"
             accept=".pdf,.png,.jpg,.jpeg,.webp,.doc,.docx,.dwg,.dxf"
             onChange={e => onFileChosen(e.target.files)} />

      {/* Add / edit property modal */}
      {form && (
        <div className="fixed inset-0 z-50 bg-ink/60 flex items-start justify-center overflow-y-auto p-4 sm:p-10"
             onClick={() => setForm(null)}>
          <div className="bg-card rounded-lg border border-rule w-full max-w-2xl p-5 sm:p-6"
               onClick={e => e.stopPropagation()} role="dialog" aria-label="Property form">
            <h2 className="text-base font-semibold text-ink mb-4">
              {form.id != null ? 'Edit Property' : 'Add Property'}
            </h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
              <label className="flex flex-col gap-1 sm:col-span-2">
                <span className="text-ghost">Name *</span>
                <input value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Type *</span>
                <select value={form.property_type}
                        onChange={e => setForm({ ...form, property_type: e.target.value as 'land' | 'building' })}
                        className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink">
                  <option value="land">Land</option>
                  <option value="building">Building</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Holding entity *</span>
                <select value={form.holder_id}
                        onChange={e => setForm({ ...form, holder_id: e.target.value ? Number(e.target.value) : '' })}
                        className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink">
                  <option value="">— select —</option>
                  <optgroup label="Entities">
                    {mainHolders.map(h => <option key={h.id} value={h.id}>{h.name}</option>)}
                  </optgroup>
                  <optgroup label="Parent Companies">
                    {parentHolders.map(h => <option key={h.id} value={h.id}>{h.name}</option>)}
                  </optgroup>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Location</span>
                <input value={form.location} onChange={e => setForm({ ...form, location: e.target.value })}
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Taluka</span>
                <input value={form.taluka} onChange={e => setForm({ ...form, taluka: e.target.value })}
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Area</span>
                <div className="flex gap-1.5">
                  <input value={form.area} onChange={e => setForm({ ...form, area: e.target.value })}
                         type="number" min="0" step="any"
                         className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink w-full" />
                  <select value={form.area_unit} onChange={e => setForm({ ...form, area_unit: e.target.value })}
                          className="bg-page border border-rule rounded px-1.5 py-1.5 text-ink">
                    <option value="sq m">sq m</option>
                    <option value="sq ft">sq ft</option>
                    <option value="acre">acre</option>
                    <option value="hectare">hectare</option>
                  </select>
                </div>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Deed no.</span>
                <input value={form.deed_no} onChange={e => setForm({ ...form, deed_no: e.target.value })}
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Date of acquisition</span>
                <input value={form.acquisition_date} type="date"
                       onChange={e => setForm({ ...form, acquisition_date: e.target.value })}
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Ownership</span>
                <input value={form.ownership} onChange={e => setForm({ ...form, ownership: e.target.value })}
                       placeholder="e.g. Sole, Joint 50%"
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">RRR (circle rate, ₹ per {form.area_unit})</span>
                <input value={form.rrr} onChange={e => setForm({ ...form, rrr: e.target.value })}
                       type="number" min="0" step="any"
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <div className="flex flex-col gap-1">
                <span className="text-ghost">Fair value ({fairMult}× RRR × area)</span>
                <span className="px-2.5 py-1.5 text-ink font-semibold tabular-nums">{fmtINR(formFair)}</span>
              </div>
              <label className="flex flex-col gap-1 sm:col-span-2">
                <span className="text-ghost">Notes</span>
                <textarea value={form.notes} onChange={e => setForm({ ...form, notes: e.target.value })}
                          rows={2} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
            </div>
            {formErr && <p role="alert" className="text-xs text-red-500 mt-3">{formErr}</p>}
            <div className="flex justify-end gap-2 mt-5">
              <button onClick={() => setForm(null)}
                      className="text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Cancel</button>
              <button onClick={saveProperty} disabled={busy === 'save'}
                      className="text-xs bg-prime text-prime-fg px-4 py-1.5 rounded font-medium hover:opacity-90 transition-opacity disabled:opacity-50">
                {busy === 'save' ? 'Saving…' : 'Save'}
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

// One property = the sheet row + (when expanded) the document checklist row.
function PropertyRows({ p, types, isOpen, isAdmin, requiredN, missingN, valReports,
                        uploadSel, busy, onToggle, onEdit, onDelete, onSelectDoc, onUpload, onDeleteDoc }: {
  p: Property; types: DocType[]; isOpen: boolean; isAdmin: boolean;
  requiredN: number; missingN: number; valReports: PropDoc[];
  uploadSel: string; busy: boolean;
  onToggle: () => void; onEdit: () => void; onDelete: () => void;
  onSelectDoc: (slug: string) => void; onUpload: (slug: string) => void;
  onDeleteDoc: (d: PropDoc) => void;
}) {
  const byType: Record<string, PropDoc[]> = {};
  for (const d of p.documents) (byType[d.doc_type] ??= []).push(d);
  const topLevel = types.filter(t => !t.parent);
  const children = (slug: string) => types.filter(t => t.parent === slug);
  const complete = missingN === 0;

  const statusIcon = (t: DocType) => {
    if ((byType[t.slug]?.length ?? 0) > 0) return <span className="text-emerald-500" title="Uploaded">●</span>;
    if (t.optional) return <span className="text-ghost" title="Optional — not uploaded">○</span>;
    return <span className="text-red-500" title="Required — missing">○</span>;
  };

  const checklistRow = (t: DocType, depth: number) => (
    <li key={t.slug} className={`flex items-start gap-2 py-1 ${depth ? 'pl-5' : ''}`}>
      <span className="mt-0.5">{statusIcon(t)}</span>
      <div className="min-w-0 flex-1">
        <span className={`${(byType[t.slug]?.length ?? 0) > 0 ? 'text-ink' : 'text-dim'}`}>
          {t.label}{t.optional && <span className="text-ghost"> (if any)</span>}
        </span>
        {(byType[t.slug] ?? []).map(d => (
          <span key={d.id} className="ml-2 inline-flex items-center gap-1">
            <a href={docUrl(d.id)} target="_blank" rel="noopener noreferrer"
               className="text-prime hover:underline break-all">
              {d.original_name || 'file'}{d.converted ? ' (PDF)' : ''}
            </a>
            {d.has_original && (
              <a href={docUrl(d.id, true)} target="_blank" rel="noopener noreferrer"
                 className="text-[10px] text-ghost hover:text-ink" title="Download original upload">orig</a>
            )}
            {isAdmin && (
              <button onClick={() => onDeleteDoc(d)} aria-label={`Delete ${d.original_name || 'document'}`}
                      className="text-ghost hover:text-red-500 text-[10px]">✕</button>
            )}
          </span>
        ))}
      </div>
      {isAdmin && (
        <button onClick={() => onUpload(t.slug)} disabled={busy}
                className="shrink-0 text-[10px] border border-wire text-dim px-1.5 py-0.5 rounded hover:border-dim hover:text-ink transition-colors disabled:opacity-50">
          Upload
        </button>
      )}
    </li>
  );

  return (
    <>
      <tr className="border-b border-rule align-top hover:bg-page/50 transition-colors">
        <td className="px-3 py-2.5">
          <p className="font-medium text-ink">{p.name}</p>
          <span className={`inline-block mt-0.5 text-[10px] uppercase tracking-wide px-1.5 py-px rounded border ${
            p.property_type === 'building' ? 'border-sky-500/40 text-sky-500' : 'border-emerald-500/40 text-emerald-500'}`}>
            {p.property_type}
          </span>
        </td>
        <td className="px-3 py-2.5 text-dim">{p.holder_name}</td>
        <td className="px-3 py-2.5 text-dim">{p.location || '—'}</td>
        <td className="px-3 py-2.5 text-dim">{p.taluka || '—'}</td>
        <td className="px-3 py-2.5 text-right tabular-nums text-dim">
          {p.area != null ? `${p.area.toLocaleString('en-IN')} ${p.area_unit || ''}` : '—'}
        </td>
        <td className="px-3 py-2.5 text-dim">{p.deed_no || '—'}</td>
        <td className="px-3 py-2.5 text-dim whitespace-nowrap">{p.acquisition_date || '—'}</td>
        <td className="px-3 py-2.5 text-dim">{p.ownership || '—'}</td>
        <td className="px-3 py-2.5 text-right tabular-nums text-dim">{fmtINR(p.rrr)}</td>
        <td className="px-3 py-2.5 text-right tabular-nums font-semibold text-ink">{fmtINR(p.fair_value)}</td>
        <td className="px-3 py-2.5">
          {valReports.length > 0 ? valReports.map(d => (
            <a key={d.id} href={docUrl(d.id)} target="_blank" rel="noopener noreferrer"
               className="block text-prime hover:underline truncate max-w-[140px]">
              📄 {d.original_name || 'report'}
            </a>
          )) : isAdmin ? (
            <button onClick={() => onUpload('valuation_report')} disabled={busy}
                    className="text-[10px] border border-wire text-dim px-1.5 py-0.5 rounded hover:border-dim hover:text-ink transition-colors disabled:opacity-50">
              Upload
            </button>
          ) : <span className="text-ghost">—</span>}
        </td>
        <td className="px-3 py-2.5">
          <button onClick={onToggle} aria-expanded={isOpen}
                  className="flex items-center gap-1.5 text-dim hover:text-ink transition-colors">
            <span className={`inline-block w-2 h-2 rounded-full ${complete ? 'bg-emerald-500' : 'bg-red-500'}`}
                  title={complete ? 'All required documents uploaded' : `${missingN} required document${missingN === 1 ? '' : 's'} missing`} />
            <span className="tabular-nums">{requiredN - missingN}/{requiredN}</span>
            <span className="text-ghost">{isOpen ? '▾' : '▸'}</span>
          </button>
        </td>
        {isAdmin && (
          <td className="px-3 py-2.5 whitespace-nowrap">
            <button onClick={onEdit} className="text-ghost hover:text-ink text-[11px] mr-2">Edit</button>
            <button onClick={onDelete} className="text-ghost hover:text-red-500 text-[11px]">Delete</button>
          </td>
        )}
      </tr>

      {isOpen && (
        <tr className="border-b border-rule bg-page/40">
          <td colSpan={isAdmin ? 13 : 12} className="px-4 sm:px-6 py-3">
            <div className="flex flex-wrap items-center justify-between gap-2 mb-2">
              <p className="text-[11px] uppercase tracking-wide text-ghost">
                Document checklist — {p.property_type}
                {missingN > 0 && <span className="text-red-500 normal-case tracking-normal"> · {missingN} required missing</span>}
              </p>
              {isAdmin && (
                <span className="flex items-center gap-1.5">
                  <select value={uploadSel} onChange={e => onSelectDoc(e.target.value)}
                          aria-label="Document type to upload"
                          className="text-xs bg-card border border-rule rounded px-2 py-1 text-ink max-w-[260px]">
                    <option value="">Add a document…</option>
                    {topLevel.map(t => (
                      <optgroup key={t.slug} label={t.label}>
                        <option value={t.slug}>
                          {t.label}{(byType[t.slug]?.length ?? 0) > 0 ? ' ✓' : t.optional ? ' (if any)' : ' — missing'}
                        </option>
                        {children(t.slug).map(c => (
                          <option key={c.slug} value={c.slug}>
                            &nbsp;&nbsp;{c.label}{(byType[c.slug]?.length ?? 0) > 0 ? ' ✓' : c.optional ? ' (if any)' : ' — missing'}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                  <button onClick={() => onUpload(uploadSel)} disabled={busy || !uploadSel}
                          className="text-xs bg-prime text-prime-fg px-2.5 py-1 rounded font-medium hover:opacity-90 transition-opacity disabled:opacity-50">
                    {busy ? 'Uploading…' : 'Upload'}
                  </button>
                </span>
              )}
            </div>
            <ul className="columns-1 md:columns-2 xl:columns-3 gap-8 text-xs [&>li]:break-inside-avoid">
              {topLevel.map(t => (
                <li key={t.slug} className="mb-0.5">
                  <ul>
                    {checklistRow(t, 0)}
                    {children(t.slug).map(c => checklistRow(c, 1))}
                  </ul>
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}
