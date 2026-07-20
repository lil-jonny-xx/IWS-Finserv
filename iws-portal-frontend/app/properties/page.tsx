'use client';
import { useEffect, useMemo, useRef, useState, useCallback, type ReactNode } from 'react';
import { useRouter } from 'next/navigation';
import PhotoLightbox from '@/app/components/PhotoLightbox';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

const PROPERTY_TYPES = ['land', 'building', 'apartment', 'godown', 'shop'] as const;
type PropertyType = typeof PROPERTY_TYPES[number];
const BUILDING_LIKE: PropertyType[] = ['building', 'apartment', 'godown', 'shop'];
const isBuildingLike = (t: PropertyType) => BUILDING_LIKE.includes(t);

interface User { role: string; full_name: string; entity_id?: number; }
interface Holder { id: number; name: string; short_code: string | null; grp: 'main' | 'parent'; is_custom: boolean; }
interface NatureType { id: number; name: string; is_custom: boolean; }
interface DocType { slug: string; label: string; scope: 'land' | 'building'; optional: boolean; parent: string | null; }
interface PropDoc {
  id: number; doc_type: string; floor_id: number | null;
  original_name: string | null; custom_label: string | null; mime: string | null;
  size_bytes: number | null; converted: boolean; has_original: boolean; uploaded_at: string | null;
}
interface Owner { holder_id: number; name: string; pct: number; }
interface Nature { nature_id: number; name: string; area: number | null; }
interface PropImage { id: number; caption: string | null; is_hero: boolean; has_thumb: boolean; }
interface Floor {
  id: number; floor_label: string; area: number | null;
  rate_per_unit: number | null; built_up_area: number | null; carpet_area: number | null;
  is_rented: boolean; rent_amount: number | null; tenant: string | null; floor_value: number | null;
}
interface Property {
  id: number; name: string; property_type: PropertyType;
  holder_id: number; holder_name: string;
  owners: Owner[]; natures: Nature[]; floors: Floor[]; images: PropImage[];
  village: string | null; address: string | null; taluka: string | null;
  survey_no: string | null; gps_link: string | null; maps_link: string | null; bhunaksha_url: string | null;
  area: number | null; built_up_area: number | null; area_unit: string | null; property_no: string | null;
  acquisition_date: string | null; ownership: string | null;
  tenure: string | null; is_old_lease: boolean;
  has_parking: boolean; parking_count: number | null;
  seller_name: string | null; seller_address: string | null;
  stamp_value: number | null; lawyer_fees: number | null;
  purchase_price: number | null; market_value: number | null;
  rrr: number | null; fair_value: number | null; building_value: number | null;
  total_value: number | null; value_effective: number | null;
  sold: boolean; sale_price: number | null; sale_date: string | null;
  notes: string | null;
  documents: PropDoc[]; missing_required: string[];
}
interface PropertiesResponse {
  count: number; total_fair_value: number; total_sold_value: number; properties: Property[];
}

interface OwnerForm { holder_id: number | ''; pct: string; }
interface NatureForm { nature_id: number | ''; area: string; }
interface FloorForm {
  id: number | null; floor_label: string; area: string; rate_per_unit: string;
  built_up_area: string; carpet_area: string; is_rented: boolean; rent_amount: string; tenant: string;
}
interface PropertyForm {
  id: number | null; name: string; property_type: PropertyType;
  owners: OwnerForm[]; natures: NatureForm[]; floors: FloorForm[];
  village: string; address: string; taluka: string; survey_no: string; gps_link: string;
  area: string; built_up_area: string; area_unit: string; property_no: string;
  acquisition_date: string; ownership: string; tenure: string; is_old_lease: boolean;
  has_parking: boolean; parking_count: string;
  seller_name: string; seller_address: string; stamp_value: string; lawyer_fees: string;
  purchase_price: string; market_value: string; rrr: string; notes: string;
}
const EMPTY_FORM: PropertyForm = {
  id: null, name: '', property_type: 'land',
  owners: [{ holder_id: '', pct: '100' }], natures: [], floors: [],
  village: '', address: '', taluka: '', survey_no: '', gps_link: '',
  area: '', built_up_area: '', area_unit: 'sq m', property_no: '',
  acquisition_date: '', ownership: '', tenure: '', is_old_lease: false,
  has_parking: false, parking_count: '',
  seller_name: '', seller_address: '', stamp_value: '', lawyer_fees: '',
  purchase_price: '', market_value: '', rrr: '', notes: '',
};

function ownersLabel(p: Property): string {
  if (!p.owners || p.owners.length === 0) return p.holder_name;
  if (p.owners.length === 1) return p.owners[0].name;
  return p.owners.map(o => `${o.name} ${o.pct % 1 === 0 ? o.pct.toFixed(0) : o.pct}%`).join(' · ');
}
function fmtINR(n: number | null | undefined): string {
  if (n == null) return '—';
  return '₹' + Math.round(n).toLocaleString('en-IN');
}
function fmtArea(n: number | null | undefined, unit: string | null): string {
  if (n == null) return '—';
  return `${n.toLocaleString('en-IN')} ${unit || ''}`.trim();
}
function docUrl(id: number, original = false) {
  return `${API_URL}/api/v1/property-documents/${id}/file${original ? '?original=true' : ''}`;
}
function imgUrl(id: number, thumb = false) {
  return `${API_URL}/api/v1/property-images/${id}/${thumb ? 'thumb' : 'file'}`;
}
function holderLabel(h: Holder) { return h.short_code || h.name; }

// Document link text hides the real filename: a doc shows as its class label,
// numbered (e.g. "Zoning Certificate 1/2") when several of the class exist, or
// the admin-supplied custom name when set.
function docDisplayName(d: PropDoc, sameType: PropDoc[], typeLabel: string): string {
  if (d.custom_label) return d.custom_label;
  if (sameType.length <= 1) return typeLabel;
  const idx = sameType.findIndex(x => x.id === d.id);
  return `${typeLabel} ${idx + 1}`;
}

export default function PropertiesPage() {
  const router = useRouter();
  const [user, setUser]         = useState<User | null>(null);
  const [holders, setHolders]   = useState<Holder[]>([]);
  const [natureTypes, setNatureTypes] = useState<NatureType[]>([]);
  const [docTypes, setDocTypes] = useState<DocType[]>([]);
  const [fairMult, setFairMult] = useState(1.75);
  const [data, setData]         = useState<PropertiesResponse | null>(null);
  const [loading, setLoading]   = useState(true);
  const [error, setError]       = useState<string | null>(null);
  const [busy, setBusy]         = useState<string | null>(null);

  // Multi-select holder filter — empty set = All (mirrors the global EntitySwitcher).
  const [selHolders, setSelHolders] = useState<Set<number>>(new Set());
  const toggleHolder = (id: number | null) => setSelHolders(prev => {
    if (id === null) return new Set();
    const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n;
  });

  const [card, setCard]         = useState<Property | null>(null);   // detail modal
  const [form, setForm]         = useState<PropertyForm | null>(null);
  const [formErr, setFormErr]   = useState<string | null>(null);
  const [formSaved, setFormSaved] = useState(false);
  const [newEntity, setNewEntity] = useState('');
  const [newNature, setNewNature] = useState('');
  const [sellFor, setSellFor]     = useState<Property | null>(null);
  const [sellPrice, setSellPrice] = useState('');
  const [sellDate, setSellDate]   = useState('');
  const [sellErr, setSellErr]     = useState<string | null>(null);
  const fileRef                 = useRef<HTMLInputElement | null>(null);
  const imgRef                  = useRef<HTMLInputElement | null>(null);
  const pendingUpload           = useRef<{ propId: number; slug: string; floorId?: number; label?: string } | null>(null);
  const pendingImage            = useRef<{ propId: number } | null>(null);

  const isAdmin = user?.role === 'admin';

  const loadStatic = useCallback(() => {
    fetch(`${API_URL}/api/v1/property-entities`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : []).then(setHolders).catch(() => {});
    fetch(`${API_URL}/api/v1/property-nature-types`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : []).then(setNatureTypes).catch(() => {});
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

  // Keep the open card fresh after any reload (e.g. after an upload).
  useEffect(() => {
    if (card && data) {
      const fresh = data.properties.find(p => p.id === card.id);
      if (fresh && fresh !== card) setCard(fresh);
    }
  }, [data]); // eslint-disable-line react-hooks/exhaustive-deps

  const mainHolders   = useMemo(() => holders.filter(h => h.grp === 'main'), [holders]);
  const parentHolders = useMemo(() => holders.filter(h => h.grp === 'parent'), [holders]);
  // Parent companies collapse into a single tab; clicking it reveals their
  // individual holder tabs (which still filter as before).
  const [parentExpanded, setParentExpanded] = useState(false);
  const parentSelCount = useMemo(
    () => parentHolders.reduce((n, h) => n + (selHolders.has(h.id) ? 1 : 0), 0),
    [parentHolders, selHolders]);

  const visible = useMemo(() => {
    const all = data?.properties ?? [];
    if (selHolders.size === 0) return all;
    const ownedBy = (p: Property, hid: number) =>
      p.holder_id === hid || (p.owners ?? []).some(o => o.holder_id === hid);
    return all.filter(p => [...selHolders].some(h => ownedBy(p, h)));
  }, [data, selHolders]);

  const active   = useMemo(() => visible.filter(p => !p.sold && !p.is_old_lease), [visible]);
  const leaseList = useMemo(() => visible.filter(p => !p.sold && p.is_old_lease), [visible]);
  const soldList = useMemo(() => visible.filter(p => p.sold), [visible]);
  const visibleTotal = useMemo(
    () => [...active, ...leaseList].reduce((s, p) => s + (p.value_effective ?? 0), 0),
    [active, leaseList]);
  const soldTotal = useMemo(
    () => soldList.reduce((s, p) => s + (p.sale_price ?? 0), 0), [soldList]);

  const docTypesFor = useCallback((type: PropertyType) =>
    isBuildingLike(type) ? docTypes : docTypes.filter(d => d.scope === 'land'), [docTypes]);
  const docLabelFor = useCallback((slug: string) =>
    docTypes.find(d => d.slug === slug)?.label ?? slug, [docTypes]);

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

  const saveNature = () => {
    const name = newNature.trim();
    if (!name) return;
    setBusy('nature');
    fetch(`${API_URL}/api/v1/property-nature-types`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
      .then(r => { if (!r.ok) throw new Error('Could not add nature'); return r.json(); })
      .then(() => { setNewNature(''); loadStatic(); })
      .catch(e => alert(e.message))
      .finally(() => setBusy(null));
  };

  const saveProperty = () => {
    if (!form) return;
    if (!form.name.trim()) { setFormErr('Name is required.'); return; }
    const owners = form.owners.filter(o => o.holder_id !== '');
    if (owners.length === 0) { setFormErr('Pick at least one owning entity.'); return; }
    const pctSum = owners.reduce((s, o) => s + (Number(o.pct) || 0), 0);
    if (Math.abs(pctSum - 100) > 0.1) { setFormErr(`Ownership must total 100% (currently ${pctSum}%).`); return; }
    const natures = form.natures.filter(n => n.nature_id !== '');
    const floors = isBuildingLike(form.property_type)
      ? form.floors.filter(f => f.floor_label.trim()) : [];
    setFormErr(null); setBusy('save');
    const primary = [...owners].sort((a, b) => Number(b.pct) - Number(a.pct))[0];
    const num = (s: string) => s ? Number(s) : null;
    const body = {
      name: form.name.trim(), property_type: form.property_type,
      holder_id: primary.holder_id,
      owners: owners.map(o => ({ holder_id: o.holder_id, pct: Number(o.pct) || 0 })),
      natures: natures.map(n => ({ nature_id: n.nature_id, area: num(n.area) })),
      floors: floors.map(f => ({
        id: f.id, floor_label: f.floor_label.trim(), area: num(f.area),
        rate_per_unit: num(f.rate_per_unit), built_up_area: num(f.built_up_area),
        carpet_area: num(f.carpet_area), is_rented: f.is_rented,
        rent_amount: num(f.rent_amount), tenant: f.tenant.trim() || null,
      })),
      village: form.village || null, address: form.address || null,
      taluka: form.taluka || null,
      survey_no: form.survey_no || null, gps_link: form.gps_link || null,
      area: num(form.area), built_up_area: num(form.built_up_area),
      area_unit: form.area_unit || 'sq m', property_no: form.property_no || null,
      acquisition_date: form.acquisition_date || null, ownership: form.ownership || null,
      tenure: form.tenure || null, is_old_lease: form.is_old_lease,
      has_parking: form.has_parking, parking_count: form.has_parking ? num(form.parking_count) : null,
      seller_name: form.seller_name || null, seller_address: form.seller_address || null,
      stamp_value: num(form.stamp_value), lawyer_fees: num(form.lawyer_fees),
      purchase_price: num(form.purchase_price), market_value: num(form.market_value),
      rrr: num(form.rrr), notes: form.notes || null,
    };
    fetch(`${API_URL}/api/v1/properties${form.id != null ? `/${form.id}` : ''}`, {
      method: form.id != null ? 'PUT' : 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
      .then(r => { if (!r.ok) return r.json().then((e: { detail?: string }) => { throw new Error(e.detail || 'Save failed'); }); return r.json(); })
      .then((res: { id?: number }) => {
        loadProperties();
        if (form.id == null && res?.id != null) setForm(f => (f ? { ...f, id: res.id! } : f));
        setFormSaved(true);
      })
      .catch(e => setFormErr(e.message))
      .finally(() => setBusy(null));
  };

  const sellProperty = () => {
    if (!sellFor) return;
    const price = Number(sellPrice);
    if (!price || price <= 0) { setSellErr('Enter the sale amount.'); return; }
    setSellErr(null); setBusy('sell');
    fetch(`${API_URL}/api/v1/properties/${sellFor.id}/sell`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sale_price: price, sale_date: sellDate || null }),
    })
      .then(r => { if (!r.ok) return r.json().then((e: { detail?: string }) => { throw new Error(e.detail || 'Could not mark sold'); }); return r.json(); })
      .then(() => { setSellFor(null); setSellPrice(''); setSellDate(''); loadProperties(); })
      .catch(e => setSellErr(e.message))
      .finally(() => setBusy(null));
  };

  const unsellProperty = (p: Property) => {
    if (!window.confirm(`Move "${p.name}" back to active (undo the sale)?`)) return;
    fetch(`${API_URL}/api/v1/properties/${p.id}/unsell`, { method: 'POST', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Undo failed'); loadProperties(); })
      .catch(e => alert(e.message));
  };

  const deleteProperty = (p: Property) => {
    if (!window.confirm(`Delete "${p.name}" and all its documents & images?`)) return;
    fetch(`${API_URL}/api/v1/properties/${p.id}`, { method: 'DELETE', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Delete failed'); setCard(null); loadProperties(); })
      .catch(e => alert(e.message));
  };

  const startUpload = (propId: number, slug: string, floorId?: number, label?: string) => {
    if (!slug) { alert('Choose a document type first.'); return; }
    pendingUpload.current = { propId, slug, floorId, label };
    fileRef.current?.click();
  };

  const onFileChosen = (files: FileList | null) => {
    const target = pendingUpload.current;
    pendingUpload.current = null;
    if (!files || files.length === 0 || !target) return;
    const fd = new FormData();
    fd.append('doc_type', target.slug);
    if (target.floorId != null) fd.append('floor_id', String(target.floorId));
    if (target.label) fd.append('custom_label', target.label);
    fd.append('file', files[0]);
    setBusy(`upload-${target.propId}`);
    fetch(`${API_URL}/api/v1/properties/${target.propId}/documents`, {
      method: 'POST', credentials: 'include', body: fd,
    })
      .then(async r => {
        if (!r.ok) {
          let detail = '';
          try { detail = (await r.json())?.detail || ''; } catch { /* non-JSON */ }
          throw new Error(detail || (r.status === 413
            ? 'File too large for the server upload limit (max 200 MB).'
            : `Upload failed (HTTP ${r.status})`));
        }
        return r.json();
      })
      .then(() => loadProperties())
      .catch(e => alert(e.message))
      .finally(() => { setBusy(null); if (fileRef.current) fileRef.current.value = ''; });
  };

  const startImageUpload = (propId: number) => {
    pendingImage.current = { propId };
    imgRef.current?.click();
  };
  const onImageChosen = (files: FileList | null) => {
    const target = pendingImage.current;
    pendingImage.current = null;
    if (!files || files.length === 0 || !target) return;
    setBusy(`img-${target.propId}`);
    // Upload sequentially so hero assignment (first image) is deterministic.
    const uploadOne = (i: number): Promise<void> => {
      if (i >= files.length) return Promise.resolve();
      const fd = new FormData();
      fd.append('file', files[i]);
      return fetch(`${API_URL}/api/v1/properties/${target.propId}/images`, {
        method: 'POST', credentials: 'include', body: fd,
      }).then(r => { if (!r.ok) throw new Error(`Image upload failed (HTTP ${r.status})`); })
        .then(() => uploadOne(i + 1));
    };
    uploadOne(0)
      .then(() => loadProperties())
      .catch(e => alert(e.message))
      .finally(() => { setBusy(null); if (imgRef.current) imgRef.current.value = ''; });
  };

  const setHero = (imgId: number, propId: number) => {
    fetch(`${API_URL}/api/v1/property-images/${imgId}/hero`, { method: 'POST', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Could not set cover'); loadProperties(); })
      .catch(e => alert(e.message));
  };
  const deleteImage = (imgId: number) => {
    if (!window.confirm('Delete this image?')) return;
    fetch(`${API_URL}/api/v1/property-images/${imgId}`, { method: 'DELETE', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Delete failed'); loadProperties(); })
      .catch(e => alert(e.message));
  };

  const deleteDoc = (d: PropDoc) => {
    if (!window.confirm(`Delete this document?`)) return;
    fetch(`${API_URL}/api/v1/property-documents/${d.id}`, { method: 'DELETE', credentials: 'include' })
      .then(r => { if (!r.ok) throw new Error('Delete failed'); loadProperties(); })
      .catch(e => alert(e.message));
  };

  // ---- render helpers -----------------------------------------------------

  const holderPill = (id: number | null, label: string) => {
    const activePill = id === null ? selHolders.size === 0 : selHolders.has(id);
    return (
      <button key={id ?? 'all'} role="tab" aria-selected={activePill}
        onClick={() => toggleHolder(id)}
        className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
          activePill ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
        {label}
      </button>
    );
  };

  const editForm = (p: Property) => setForm({
    id: p.id, name: p.name, property_type: p.property_type,
    owners: (p.owners?.length ? p.owners : [{ holder_id: p.holder_id, name: p.holder_name, pct: 100 }])
      .map(o => ({ holder_id: o.holder_id, pct: String(o.pct) })),
    natures: (p.natures ?? []).map(n => ({ nature_id: n.nature_id, area: n.area != null ? String(n.area) : '' })),
    floors: (p.floors ?? []).map(f => ({
      id: f.id, floor_label: f.floor_label, area: f.area != null ? String(f.area) : '',
      rate_per_unit: f.rate_per_unit != null ? String(f.rate_per_unit) : '',
      built_up_area: f.built_up_area != null ? String(f.built_up_area) : '',
      carpet_area: f.carpet_area != null ? String(f.carpet_area) : '',
      is_rented: f.is_rented, rent_amount: f.rent_amount != null ? String(f.rent_amount) : '',
      tenant: f.tenant ?? '',
    })),
    village: p.village ?? '', address: p.address ?? '', taluka: p.taluka ?? '',
    survey_no: p.survey_no ?? '', gps_link: p.gps_link ?? '',
    area: p.area != null ? String(p.area) : '', built_up_area: p.built_up_area != null ? String(p.built_up_area) : '',
    area_unit: p.area_unit ?? 'sq m', property_no: p.property_no ?? '',
    acquisition_date: p.acquisition_date ?? '', ownership: p.ownership ?? '',
    tenure: p.tenure ?? '', is_old_lease: p.is_old_lease,
    has_parking: p.has_parking, parking_count: p.parking_count != null ? String(p.parking_count) : '',
    seller_name: p.seller_name ?? '', seller_address: p.seller_address ?? '',
    stamp_value: p.stamp_value != null ? String(p.stamp_value) : '',
    lawyer_fees: p.lawyer_fees != null ? String(p.lawyer_fees) : '',
    purchase_price: p.purchase_price != null ? String(p.purchase_price) : '',
    market_value: p.market_value != null ? String(p.market_value) : '',
    rrr: p.rrr != null ? String(p.rrr) : '', notes: p.notes ?? '',
  });

  const formFair = form && form.area && form.rrr
    ? Number(form.area) * Number(form.rrr) * fairMult : null;
  const dataDocs = (pid: number): PropDoc[] =>
    data?.properties.find(x => x.id === pid)?.documents ?? [];

  const openCardActions = (p: Property) => ({
    onEdit: () => { setCard(null); setFormErr(null); setFormSaved(false); editForm(p); },
    onSell: () => { setCard(null); setSellErr(null); setSellPrice(''); setSellDate(''); setSellFor(p); },
    onDelete: () => deleteProperty(p),
  });

  return (
    <main id="main-content" className="min-h-screen bg-page p-4 sm:p-8">
      <div className="max-w-screen-2xl mx-auto">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Properties</h1>
            <span className="text-sm text-ghost">Land &amp; building register — documents, circle rates and fair values</span>
          </div>
        </div>

        {/* Multi-select holder filter (empty = All) */}
        <div className="flex flex-wrap gap-1.5 mb-4 items-center" role="tablist" aria-label="Holding entity filter">
          {holderPill(null, 'All')}
          {mainHolders.map(h => holderPill(h.id, holderLabel(h)))}
          {parentHolders.length > 0 && <span className="mx-1 h-4 w-px bg-rule" aria-hidden />}
          {parentHolders.length > 0 && (
            <button role="tab" aria-selected={parentSelCount > 0} aria-expanded={parentExpanded}
              onClick={() => setParentExpanded(v => !v)}
              className={`px-3 py-1 rounded text-xs font-medium transition-colors inline-flex items-center gap-1 ${
                parentSelCount > 0 ? 'bg-prime text-prime-fg' : 'bg-card border border-rule text-dim hover:border-dim hover:text-ink'}`}>
              <span aria-hidden className="text-[9px] leading-none">{parentExpanded ? '▾' : '▸'}</span>
              Parent Companies{parentSelCount > 0 ? ` (${parentSelCount})` : ''}
            </button>
          )}
          {parentExpanded && parentHolders.map(h => holderPill(h.id, holderLabel(h)))}
          {isAdmin && (
            <span className="flex items-center gap-1.5 ml-2">
              <input value={newEntity} onChange={e => setNewEntity(e.target.value)}
                     onKeyDown={e => { if (e.key === 'Enter') saveEntity(); }}
                     placeholder="Add parent company…"
                     className="text-xs bg-card border border-rule rounded px-2 py-1 text-ink w-40" />
              <button onClick={saveEntity} disabled={busy === 'entity' || !newEntity.trim()}
                      className="text-xs border border-wire text-dim px-2 py-1 rounded hover:border-dim hover:text-ink transition-colors disabled:opacity-50">
                + Add
              </button>
            </span>
          )}
        </div>

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
                <p className="text-[11px] uppercase tracking-wide text-ghost">Portfolio Value</p>
                <p className="text-2xl font-bold text-ink tabular-nums">{fmtINR(visibleTotal)}</p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-ghost">Properties</p>
                <p className="text-base font-semibold text-ink tabular-nums">{active.length + leaseList.length}</p>
              </div>
              {leaseList.length > 0 && (
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Old-lease ({leaseList.length})</p>
                  <p className="text-base font-semibold text-ink tabular-nums">50% owner share</p>
                </div>
              )}
              {soldList.length > 0 && (
                <div>
                  <p className="text-[11px] uppercase tracking-wide text-ghost">Sold ({soldList.length})</p>
                  <p className="text-base font-semibold text-ink tabular-nums">{fmtINR(soldTotal)}</p>
                </div>
              )}
              {isAdmin && (
                <button onClick={() => { setFormErr(null); setFormSaved(false); setForm({ ...EMPTY_FORM, owners: [{ holder_id: '', pct: '100' }] }); }}
                        className="ml-auto text-xs bg-prime text-prime-fg px-3 py-1.5 rounded font-medium hover:opacity-90 transition-opacity">
                  + Add Property
                </button>
              )}
            </div>

            {/* Active properties — list view; a row click opens the full detail card. */}
            <PropertyTable rows={active} onOpen={setCard}
              emptyText={`No properties recorded${selHolders.size ? ' for this filter' : ''} yet.`} />

            {leaseList.length > 0 && (
              <section className="mt-8">
                <h2 className="text-sm font-semibold text-ink mb-1 flex items-center gap-2">
                  Lease (old statutory)
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-px rounded border border-violet-500/40 text-violet-400">
                    {leaseList.length}
                  </span>
                </h2>
                <p className="text-[11px] text-ghost mb-3 max-w-3xl">
                  Pre-1990 rent-controlled leaseholds. The sitting tenant holds ~50% of the value, so only the
                  owner&apos;s half feeds the portfolio total (full value shown alongside).
                </p>
                <PropertyTable rows={leaseList} onOpen={setCard} lease />
              </section>
            )}

            {soldList.length > 0 && (
              <section className="mt-8">
                <h2 className="text-sm font-semibold text-ink mb-2 flex items-center gap-2">
                  Sold
                  <span className="text-[10px] uppercase tracking-wide px-1.5 py-px rounded border border-amber-500/40 text-amber-500">
                    {soldList.length}
                  </span>
                </h2>
                <PropertyTable rows={soldList} onOpen={setCard} sold />
                <p className="text-[11px] text-ghost mt-2">
                  Sold properties no longer count in the portfolio total — their sale price feeds Realised Gains and the overview.
                </p>
              </section>
            )}
          </div>
        )}

        <p className="text-center text-xs text-ghost mt-8">Rajani MIS &copy; {new Date().getFullYear()}</p>
      </div>

      {/* Detail card modal (Airbnb-style) */}
      {card && (
        <PropertyModal p={card} isAdmin={isAdmin} busy={busy}
          docTypesFor={docTypesFor} docLabelFor={docLabelFor}
          onClose={() => setCard(null)}
          onUploadDoc={(slug, floorId) => startUpload(card.id, slug, floorId)}
          onDeleteDoc={deleteDoc}
          onUploadImage={() => startImageUpload(card.id)}
          onSetHero={id => setHero(id, card.id)}
          onDeleteImage={deleteImage}
          {...openCardActions(card)} />
      )}

      {/* Mark-sold dialog */}
      {sellFor && (
        <div className="fixed inset-0 z-50 bg-ink/60 flex items-center justify-center p-4" onClick={() => setSellFor(null)}>
          <div className="bg-card rounded-lg border border-rule w-full max-w-sm p-5" onClick={e => e.stopPropagation()} role="dialog" aria-label="Mark property sold">
            <h2 className="text-base font-semibold text-ink mb-1">Mark as sold</h2>
            <p className="text-xs text-ghost mb-4">{sellFor.name} — {sellFor.holder_name}</p>
            <div className="flex flex-col gap-3 text-xs">
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Sale amount (₹) *</span>
                <input value={sellPrice} onChange={e => setSellPrice(e.target.value)} type="number" min="0" step="any" autoFocus
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-ghost">Sale date (defaults to today)</span>
                <input value={sellDate} type="date" onChange={e => setSellDate(e.target.value)}
                       className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
              </label>
            </div>
            {sellErr && <p role="alert" className="text-xs text-red-500 mt-3">{sellErr}</p>}
            <div className="flex justify-end gap-2 mt-5">
              <button onClick={() => setSellFor(null)} className="text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink transition-colors">Cancel</button>
              <button onClick={sellProperty} disabled={busy === 'sell'} className="text-xs bg-prime text-prime-fg px-4 py-1.5 rounded font-medium hover:opacity-90 transition-opacity disabled:opacity-50">
                {busy === 'sell' ? 'Saving…' : 'Mark Sold'}
              </button>
            </div>
          </div>
        </div>
      )}

      <input ref={fileRef} type="file" className="hidden" aria-hidden="true"
             accept=".pdf,.png,.jpg,.jpeg,.webp,.doc,.docx,.dwg,.dxf"
             onChange={e => onFileChosen(e.target.files)} />
      <input ref={imgRef} type="file" className="hidden" aria-hidden="true" multiple
             accept="image/*" onChange={e => onImageChosen(e.target.files)} />

      {/* Add / edit property modal */}
      {form && (
        <PropertyFormModal
          form={form} setForm={setForm} isAdmin={isAdmin} busy={busy}
          mainHolders={mainHolders} parentHolders={parentHolders} natureTypes={natureTypes}
          fairMult={fairMult} formFair={formFair} formErr={formErr} formSaved={formSaved}
          docTypesFor={docTypesFor} docLabelFor={docLabelFor} dataDocs={dataDocs}
          newNature={newNature} setNewNature={setNewNature} saveNature={saveNature}
          onClose={() => setForm(null)} onSave={saveProperty}
          onUpload={(slug, floorId) => startUpload(form.id!, slug, floorId)}
          onDeleteDoc={deleteDoc} onUploadImage={() => startImageUpload(form.id!)} />
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// List view — one scannable row per property. Every detail (gallery, floors,
// documents, per-floor economics) lives in PropertyModal, which a row click
// opens; the row itself carries only what you'd scan a sheet for.
// ---------------------------------------------------------------------------
const COLS = [
  { key: 'name',     label: 'Name',          align: 'left'  },
  { key: 'entity',   label: 'Entity',        align: 'left'  },
  { key: 'village',  label: 'City/Village',  align: 'left'  },
  { key: 'taluka',   label: 'Taluka',        align: 'left'  },
  { key: 'area',     label: 'Area',          align: 'right' },
  { key: 'propno',   label: 'Property No.',  align: 'left'  },
  { key: 'acquired', label: 'Acquired',      align: 'left'  },
  { key: 'purchase', label: 'Purchase',      align: 'right' },
  { key: 'rrr',      label: 'RRR',           align: 'right' },
  { key: 'value',    label: 'Value',         align: 'right' },
  { key: 'docs',     label: 'Docs',          align: 'left'  },
] as const;

function PropertyTable({ rows, onOpen, sold, lease, emptyText }: {
  rows: Property[]; onOpen: (p: Property) => void;
  sold?: boolean; lease?: boolean; emptyText?: string;
}) {
  return (
    <div className="bg-card rounded-lg border border-rule overflow-x-auto">
      <table className="w-full text-xs min-w-[1100px]">
        <thead>
          <tr className="border-b border-rule text-left text-[11px] uppercase tracking-wide text-ghost">
            {COLS.map(c => (
              <th key={c.key} className={`px-3 py-2.5 ${c.align === 'right' ? 'text-right' : ''}`}>
                {c.key === 'value' && sold ? 'Sale Price' : c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={COLS.length} className="px-3 py-12 text-center text-ghost">{emptyText}</td></tr>
          )}
          {rows.map(p => (
            <tr key={p.id}
                onClick={() => onOpen(p)}
                tabIndex={0}
                role="button"
                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(p); } }}
                className="border-t border-rule cursor-pointer hover:bg-page focus:bg-page focus:outline-none transition-colors">
              <td className="px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-ink">{p.name}</span>
                  <span className={`text-[10px] uppercase tracking-wide px-1.5 py-px rounded border shrink-0 ${
                    p.property_type === 'land'
                      ? 'border-emerald-500/40 text-emerald-500'
                      : 'border-sky-500/40 text-sky-500'}`}>
                    {p.property_type}
                  </span>
                  {sold  && <span className="text-[10px] uppercase tracking-wide px-1.5 py-px rounded border border-amber-500/40 text-amber-500 shrink-0">sold</span>}
                  {lease && <span className="text-[10px] uppercase tracking-wide px-1.5 py-px rounded border border-violet-500/40 text-violet-400 shrink-0">old lease</span>}
                  {p.images.length > 0 && (
                    <span className="text-[10px] text-ghost shrink-0">{p.images.length}📷</span>
                  )}
                </div>
              </td>
              <td className="px-3 py-2.5 text-dim">{ownersLabel(p)}</td>
              <td className="px-3 py-2.5 text-dim">{p.village || '—'}</td>
              <td className="px-3 py-2.5 text-dim">{p.taluka || '—'}</td>
              <td className="px-3 py-2.5 text-right text-dim tabular-nums">{fmtArea(p.area, p.area_unit)}</td>
              <td className="px-3 py-2.5 text-dim">{p.property_no || '—'}</td>
              <td className="px-3 py-2.5 text-dim">{sold ? (p.sale_date || '—') : (p.acquisition_date || '—')}</td>
              <td className="px-3 py-2.5 text-right text-dim tabular-nums">{fmtINR(p.purchase_price)}</td>
              <td className="px-3 py-2.5 text-right text-dim tabular-nums">{p.rrr != null ? fmtINR(p.rrr) : '—'}</td>
              <td className="px-3 py-2.5 text-right font-semibold text-ink tabular-nums">
                {fmtINR(sold ? p.sale_price : p.value_effective)}
                {lease && p.total_value != null && (
                  <span className="block text-[10px] font-normal text-ghost">of {fmtINR(p.total_value)}</span>
                )}
              </td>
              <td className="px-3 py-2.5 text-dim">
                {p.documents.length}
                {p.missing_required.length > 0 && (
                  <span className="ml-1 text-[10px] text-amber-500">{p.missing_required.length} missing</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail modal — gallery + every field + floors + documents.
// ---------------------------------------------------------------------------
function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-wide text-ghost">{label}</p>
      <p className="text-sm text-ink font-medium break-words">{children}</p>
    </div>
  );
}

function PropertyModal({ p, isAdmin, busy, docTypesFor, docLabelFor, onClose, onEdit, onSell, onDelete,
  onUploadDoc, onDeleteDoc, onUploadImage, onSetHero, onDeleteImage }: {
  p: Property; isAdmin: boolean; busy: string | null;
  docTypesFor: (t: PropertyType) => DocType[]; docLabelFor: (slug: string) => string;
  onClose: () => void; onEdit: () => void; onSell: () => void; onDelete: () => void;
  onUploadDoc: (slug: string, floorId?: number) => void; onDeleteDoc: (d: PropDoc) => void;
  onUploadImage: () => void; onSetHero: (id: number) => void; onDeleteImage: (id: number) => void;
}) {
  const hero0 = p.images.find(i => i.is_hero) || p.images[0] || null;
  const [heroId, setHeroId] = useState<number | null>(hero0 ? hero0.id : null);
  useEffect(() => {
    const h = p.images.find(i => i.is_hero) || p.images[0] || null;
    setHeroId(h ? h.id : null);
  }, [p.id]); // eslint-disable-line react-hooks/exhaustive-deps
  const heroImg = p.images.find(i => i.id === heroId) || hero0;
  const [zoom, setZoom] = useState<number | null>(null);

  const types = docTypesFor(p.property_type);
  const byType: Record<string, PropDoc[]> = {};
  for (const d of p.documents) (byType[d.doc_type] ??= []).push(d);

  return (
    <div className="fixed inset-0 z-50 bg-ink/70 flex items-start justify-center overflow-y-auto p-4 sm:p-8" onClick={onClose}>
      <div className="bg-card rounded-lg border border-rule w-full max-w-4xl" onClick={e => e.stopPropagation()} role="dialog" aria-label={p.name}>
        {/* Gallery */}
        <div className="p-4 sm:p-6">
          <div className="flex items-start justify-between gap-3 mb-3">
            <div>
              <h2 className="text-lg font-bold text-ink">{p.name}</h2>
              <p className="text-xs text-ghost">{ownersLabel(p)}</p>
            </div>
            <button onClick={onClose} aria-label="Close" className="text-ghost hover:text-ink text-xl leading-none">×</button>
          </div>

          {/* Deliberately a modest fixed height, not a full-width 16:9 hero — the
              details are what this card is for; the photo is supporting context. */}
          <div className="w-full h-44 sm:h-52 bg-page rounded-lg overflow-hidden mb-2 flex items-center justify-center">
            {heroImg ? (
              <button
                onClick={() => setZoom(heroImg.id)}
                className="w-full h-full flex items-center justify-center cursor-zoom-in"
                aria-label={`Zoom into photo of ${p.name}`}
              >
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={imgUrl(heroImg.id)} alt={p.name} className="max-w-full max-h-full object-contain" />
              </button>
            ) : (
              <span className="text-ghost text-xs">No images uploaded</span>
            )}
          </div>
          <div className="flex flex-wrap gap-2 items-center">
            {p.images.map(im => (
              <div key={im.id} className="relative group">
                <button onClick={() => setHeroId(im.id)}
                        className={`w-12 h-12 rounded overflow-hidden border-2 ${im.id === heroId ? 'border-prime' : 'border-transparent'}`}>
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={imgUrl(im.id, true)} alt="" className="w-full h-full object-cover" />
                </button>
                {isAdmin && (
                  <span className="absolute -top-1 -right-1 hidden group-hover:flex gap-0.5">
                    {!im.is_hero && (
                      <button onClick={() => onSetHero(im.id)} title="Set as cover"
                              className="text-[9px] bg-card border border-rule rounded px-1 text-dim hover:text-ink">★</button>
                    )}
                    <button onClick={() => onDeleteImage(im.id)} title="Delete image"
                            className="text-[9px] bg-card border border-rule rounded px-1 text-dim hover:text-red-500">✕</button>
                  </span>
                )}
              </div>
            ))}
            {isAdmin && (
              <button onClick={onUploadImage} disabled={busy === `img-${p.id}`}
                      className="w-16 h-16 rounded border border-dashed border-wire text-ghost hover:text-ink hover:border-dim text-xs disabled:opacity-50">
                {busy === `img-${p.id}` ? '…' : '+ Photo'}
              </button>
            )}
          </div>
        </div>

        {/* Details */}
        <div className="px-4 sm:px-6 pb-5 grid grid-cols-2 sm:grid-cols-3 gap-x-5 gap-y-4 border-t border-rule pt-4">
          <Field label="Type">{p.property_type}</Field>
          <Field label="Nature">{p.natures.length ? p.natures.map(n => n.area != null ? `${n.name} (${fmtArea(n.area, p.area_unit)})` : n.name).join(', ') : '—'}</Field>
          <Field label="Tenure">{p.tenure || '—'}{p.is_old_lease ? ' · old lease' : ''}</Field>
          <Field label="Ownership">{p.ownership || '—'}</Field>
          <Field label="City/Village">{p.village || '—'}</Field>
          <Field label="Address">{p.address || '—'}</Field>
          <Field label="Taluka">{p.taluka || '—'}</Field>
          <Field label="Total area">{fmtArea(p.area, p.area_unit)}</Field>
          <Field label="Built-up area">{fmtArea(p.built_up_area, p.area_unit)}</Field>
          <Field label="Property no.">{p.property_no || '—'}</Field>
          <Field label="Survey no.">{p.survey_no || '—'}</Field>
          <Field label="Acquired">{p.acquisition_date || '—'}</Field>
          <Field label="Car parking">{p.has_parking ? (p.parking_count != null ? `${p.parking_count} space(s)` : 'Yes') : 'No'}</Field>
          <Field label="Location links">
            <span className="flex flex-wrap gap-2">
              {p.maps_link ? <a href={p.maps_link} target="_blank" rel="noopener noreferrer" className="text-prime hover:underline">Google Maps</a> : <span className="text-ghost">Maps —</span>}
              {p.bhunaksha_url && <a href={p.bhunaksha_url} target="_blank" rel="noopener noreferrer" className="text-prime hover:underline" title={`Survey ${p.survey_no}${p.village ? ', ' + p.village : ''} — select in portal`}>Bhunaksha</a>}
            </span>
          </Field>
          <Field label="RRR (circle rate)">{fmtINR(p.rrr)}</Field>
          <Field label="Fair value (land)">{fmtINR(p.fair_value)}</Field>
          {p.building_value != null && <Field label="Building value">{fmtINR(p.building_value)}</Field>}
          <Field label="Market value">{fmtINR(p.market_value)}</Field>
          {p.is_old_lease ? (
            <Field label="Value (full · owner 50%)">{fmtINR(p.total_value)} · {fmtINR(p.value_effective)}</Field>
          ) : (
            <Field label="Total value">{fmtINR(p.total_value)}</Field>
          )}
          <Field label="Purchase price">{fmtINR(p.purchase_price)}</Field>
          {p.sold && <Field label="Sold">{p.sale_date} · {fmtINR(p.sale_price)}</Field>}
        </div>

        {p.notes && (
          <div className="px-4 sm:px-6 pb-4">
            <p className="text-[10px] uppercase tracking-wide text-ghost">Description</p>
            <p className="text-xs text-dim whitespace-pre-wrap">{p.notes}</p>
          </div>
        )}

        {/* Seller */}
        {(p.seller_name || p.seller_address || p.stamp_value != null || p.lawyer_fees != null) && (
          <div className="px-4 sm:px-6 pb-4 border-t border-rule pt-3 grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2">
            <p className="col-span-2 sm:col-span-4 text-[10px] uppercase tracking-wide text-ghost">Purchased from seller</p>
            <Field label="Seller">{p.seller_name || '—'}</Field>
            <Field label="Seller address">{p.seller_address || '—'}</Field>
            <Field label="Stamp value">{fmtINR(p.stamp_value)}</Field>
            <Field label="Lawyer fees">{fmtINR(p.lawyer_fees)}</Field>
          </div>
        )}

        {/* Floors */}
        {p.floors.length > 0 && (
          <div className="px-4 sm:px-6 pb-4 border-t border-rule pt-3">
            <p className="text-[10px] uppercase tracking-wide text-ghost mb-2">Floors</p>
            <div className="overflow-x-auto">
              <table className="w-full text-xs min-w-[640px]">
                <thead>
                  <tr className="text-left text-[10px] uppercase tracking-wide text-ghost border-b border-rule">
                    <th className="py-1.5 pr-3">Floor</th>
                    <th className="py-1.5 pr-3 text-right">Area</th>
                    <th className="py-1.5 pr-3 text-right">Built-up</th>
                    <th className="py-1.5 pr-3 text-right">Carpet</th>
                    <th className="py-1.5 pr-3 text-right">Rate</th>
                    <th className="py-1.5 pr-3 text-right">Value</th>
                    <th className="py-1.5 pr-3">Tenancy</th>
                    <th className="py-1.5">Agreement</th>
                  </tr>
                </thead>
                <tbody>
                  {p.floors.map(f => {
                    const agrees = p.documents.filter(d => d.floor_id === f.id &&
                      (d.doc_type === 'rent_agreement' || d.doc_type === 'lease_agreement'));
                    return (
                      <tr key={f.id} className="border-b border-rule/60 text-dim">
                        <td className="py-1.5 pr-3 text-ink">{f.floor_label}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{f.area ?? '—'}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{f.built_up_area ?? '—'}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{f.carpet_area ?? '—'}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums">{fmtINR(f.rate_per_unit)}</td>
                        <td className="py-1.5 pr-3 text-right tabular-nums text-ink">{fmtINR(f.floor_value)}</td>
                        <td className="py-1.5 pr-3">{f.is_rented ? `Rented${f.tenant ? ' · ' + f.tenant : ''}${f.rent_amount != null ? ' · ' + fmtINR(f.rent_amount) + '/mo' : ''}` : 'Vacant'}</td>
                        <td className="py-1.5">
                          {agrees.map(d => (
                            <a key={d.id} href={docUrl(d.id)} target="_blank" rel="noopener noreferrer" className="block text-prime hover:underline">
                              {docDisplayName(d, agrees.filter(x => x.doc_type === d.doc_type), docLabelFor(d.doc_type))}
                            </a>
                          ))}
                          {isAdmin && f.is_rented && (
                            <span className="flex gap-1 mt-0.5">
                              <button onClick={() => onUploadDoc('rent_agreement', f.id)} className="text-[10px] border border-wire text-dim px-1 rounded hover:text-ink">+rent</button>
                              <button onClick={() => onUploadDoc('lease_agreement', f.id)} className="text-[10px] border border-wire text-dim px-1 rounded hover:text-ink">+lease</button>
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Documents — labelled by class, real filenames hidden */}
        <div className="px-4 sm:px-6 pb-5 border-t border-rule pt-3">
          <p className="text-[10px] uppercase tracking-wide text-ghost mb-2">
            Documents{p.missing_required.length > 0 && <span className="text-red-500 normal-case"> · {p.missing_required.length} required missing</span>}
          </p>
          <ul className="columns-1 sm:columns-2 lg:columns-3 gap-6 text-xs [&>li]:break-inside-avoid">
            {types.filter(t => !t.parent).map(t => {
              const docs = byType[t.slug] ?? [];
              const has = docs.length > 0;
              return (
                <li key={t.slug} className="mb-1 flex items-start gap-1.5">
                  <span className={`mt-0.5 ${has ? 'text-emerald-500' : t.optional ? 'text-ghost' : 'text-red-500'}`}>{has ? '●' : '○'}</span>
                  <div className="min-w-0 flex-1">
                    <span className={has ? 'text-ink' : 'text-dim'}>{t.label}{t.optional && <span className="text-ghost"> (if any)</span>}</span>
                    {docs.map(d => (
                      <span key={d.id} className="ml-2 inline-flex items-center gap-1">
                        <a href={docUrl(d.id)} target="_blank" rel="noopener noreferrer" className="text-prime hover:underline">
                          {docDisplayName(d, docs, t.label)}{d.converted ? ' (PDF)' : ''}
                        </a>
                        {d.has_original && <a href={docUrl(d.id, true)} target="_blank" rel="noopener noreferrer" className="text-[10px] text-ghost hover:text-ink" title="Original upload">orig</a>}
                        {isAdmin && <button onClick={() => onDeleteDoc(d)} className="text-ghost hover:text-red-500 text-[10px]" aria-label="Delete document">✕</button>}
                      </span>
                    ))}
                  </div>
                  {isAdmin && (
                    <button onClick={() => onUploadDoc(t.slug)} disabled={busy === `upload-${p.id}`}
                            className="shrink-0 text-[10px] border border-wire text-dim px-1.5 py-0.5 rounded hover:border-dim hover:text-ink disabled:opacity-50">Upload</button>
                  )}
                </li>
              );
            })}
          </ul>
        </div>

        {isAdmin && (
          <div className="px-4 sm:px-6 py-3 border-t border-rule flex justify-end gap-2">
            <button onClick={onEdit} className="text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink">Edit</button>
            {!p.sold && <button onClick={onSell} className="text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-amber-500 hover:text-amber-500">Mark sold</button>}
            <button onClick={onDelete} className="text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-red-500 hover:text-red-500">Delete</button>
          </div>
        )}
      </div>

      {/* Sits above this modal (z-70 vs z-50) so the photo is unobstructed. */}
      {zoom != null && (
        <PhotoLightbox src={imgUrl(zoom)} alt={p.name} onClose={() => setZoom(null)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add / edit form modal.
// ---------------------------------------------------------------------------
function PropertyFormModal({ form, setForm, isAdmin, busy, mainHolders, parentHolders, natureTypes,
  fairMult, formFair, formErr, formSaved, docTypesFor, docLabelFor, dataDocs,
  newNature, setNewNature, saveNature, onClose, onSave, onUpload, onDeleteDoc,
  onUploadImage }: {
  form: PropertyForm; setForm: (f: PropertyForm | null) => void; isAdmin: boolean; busy: string | null;
  mainHolders: Holder[]; parentHolders: Holder[]; natureTypes: NatureType[];
  fairMult: number; formFair: number | null; formErr: string | null; formSaved: boolean;
  docTypesFor: (t: PropertyType) => DocType[]; docLabelFor: (slug: string) => string;
  dataDocs: (pid: number) => PropDoc[];
  newNature: string; setNewNature: (s: string) => void; saveNature: () => void;
  onClose: () => void; onSave: () => void; onUpload: (slug: string, floorId?: number) => void;
  onDeleteDoc: (d: PropDoc) => void; onUploadImage: () => void;
}) {
  const set = (patch: Partial<PropertyForm>) => setForm({ ...form, ...patch });
  const buildingLike = isBuildingLike(form.property_type);

  return (
    <div className="fixed inset-0 z-50 bg-ink/60 flex items-start justify-center overflow-y-auto p-4 sm:p-10" onClick={onClose}>
      <div className="bg-card rounded-lg border border-rule w-full max-w-3xl p-5 sm:p-6" onClick={e => e.stopPropagation()} role="dialog" aria-label="Property form">
        <h2 className="text-base font-semibold text-ink mb-4">{form.id != null ? 'Edit Property' : 'Add Property'}</h2>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
          <label className="flex flex-col gap-1 sm:col-span-2">
            <span className="text-ghost">Name *</span>
            <input value={form.name} onChange={e => set({ name: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" />
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-ghost">Type *</span>
            <select value={form.property_type} onChange={e => set({ property_type: e.target.value as PropertyType })}
                    className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink capitalize">
              {PROPERTY_TYPES.map(t => <option key={t} value={t} className="capitalize">{t}</option>)}
            </select>
          </label>

          <label className="flex flex-col gap-1">
            <span className="text-ghost">Ownership tenure</span>
            <select value={form.tenure} onChange={e => set({ tenure: e.target.value })}
                    className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink">
              <option value="">—</option>
              <option value="freehold">Freehold</option>
              <option value="leasehold">Leasehold</option>
            </select>
          </label>

          {form.tenure === 'leasehold' && (
            <label className="flex items-center gap-2 sm:col-span-2 text-dim">
              <input type="checkbox" checked={form.is_old_lease} onChange={e => set({ is_old_lease: e.target.checked })} />
              <span>Old statutory lease (pre-1990, rent-controlled) — shown in the Lease section; market value counts at 50% (tenant holds the other half).</span>
            </label>
          )}

          {/* Owning entities */}
          <div className="flex flex-col gap-1 sm:col-span-2">
            <span className="text-ghost">Owning entities * <span className="normal-case">(must total 100%)</span></span>
            {form.owners.map((o, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <select value={o.holder_id}
                        onChange={e => set({ owners: form.owners.map((x, j) => j === i ? { ...x, holder_id: e.target.value ? Number(e.target.value) : '' } : x) })}
                        className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink flex-1">
                  <option value="">— select entity —</option>
                  <optgroup label="Entities">{mainHolders.map(h => <option key={h.id} value={h.id}>{h.name}</option>)}</optgroup>
                  <optgroup label="Parent Companies">{parentHolders.map(h => <option key={h.id} value={h.id}>{h.name}</option>)}</optgroup>
                </select>
                <input value={o.pct} type="number" min="0" max="100" step="any" aria-label="Ownership %"
                       onChange={e => set({ owners: form.owners.map((x, j) => j === i ? { ...x, pct: e.target.value } : x) })}
                       className="bg-page border border-rule rounded px-2 py-1.5 text-ink w-20 text-right" />
                <span className="text-ghost">%</span>
                {form.owners.length > 1 && <button onClick={() => set({ owners: form.owners.filter((_, j) => j !== i) })} aria-label="Remove owner" className="text-ghost hover:text-red-500">✕</button>}
              </div>
            ))}
            <button onClick={() => set({ owners: [...form.owners, { holder_id: '', pct: '' }] })} className="self-start text-[11px] border border-wire text-dim px-2 py-0.5 rounded hover:border-dim hover:text-ink">+ Add joint owner</button>
          </div>

          {/* Nature (multi + area split + custom) */}
          <div className="flex flex-col gap-1 sm:col-span-2">
            <span className="text-ghost">Nature <span className="normal-case">(area per nature — optional)</span></span>
            {form.natures.map((n, i) => (
              <div key={i} className="flex items-center gap-1.5">
                <select value={n.nature_id}
                        onChange={e => set({ natures: form.natures.map((x, j) => j === i ? { ...x, nature_id: e.target.value ? Number(e.target.value) : '' } : x) })}
                        className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink flex-1">
                  <option value="">— select nature —</option>
                  {natureTypes.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
                </select>
                <input value={n.area} type="number" min="0" step="any" placeholder="Area" aria-label="Nature area"
                       onChange={e => set({ natures: form.natures.map((x, j) => j === i ? { ...x, area: e.target.value } : x) })}
                       className="bg-page border border-rule rounded px-2 py-1.5 text-ink w-24 text-right" />
                <span className="text-ghost">{form.area_unit}</span>
                <button onClick={() => set({ natures: form.natures.filter((_, j) => j !== i) })} aria-label="Remove nature" className="text-ghost hover:text-red-500">✕</button>
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-2">
              <button onClick={() => set({ natures: [...form.natures, { nature_id: '', area: '' }] })} className="self-start text-[11px] border border-wire text-dim px-2 py-0.5 rounded hover:border-dim hover:text-ink">+ Add nature</button>
              {isAdmin && (
                <span className="flex items-center gap-1.5">
                  <input value={newNature} onChange={e => setNewNature(e.target.value)}
                         onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); saveNature(); } }}
                         placeholder="New nature type…" className="text-[11px] bg-page border border-rule rounded px-2 py-1 text-ink w-36" />
                  <button onClick={saveNature} disabled={busy === 'nature' || !newNature.trim()} className="text-[11px] border border-wire text-dim px-2 py-1 rounded hover:border-dim hover:text-ink disabled:opacity-50">+ Type</button>
                </span>
              )}
            </div>
          </div>

          <label className="flex flex-col gap-1"><span className="text-ghost">City/Village</span>
            <input value={form.village} onChange={e => set({ village: e.target.value })} placeholder="also used for the Bhunaksha lookup"
                   className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
          <label className="flex flex-col gap-1"><span className="text-ghost">Address</span>
            <input value={form.address} onChange={e => set({ address: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
          <label className="flex flex-col gap-1"><span className="text-ghost">Taluka</span>
            <input value={form.taluka} onChange={e => set({ taluka: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>

          <label className="flex flex-col gap-1"><span className="text-ghost">Total area of land</span>
            <div className="flex gap-1.5">
              <input value={form.area} onChange={e => set({ area: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink w-full" />
              <select value={form.area_unit} onChange={e => set({ area_unit: e.target.value })} className="bg-page border border-rule rounded px-1.5 py-1.5 text-ink">
                <option value="sq m">sq m</option><option value="sq ft">sq ft</option><option value="acre">acre</option><option value="hectare">hectare</option>
              </select>
            </div>
          </label>
          <label className="flex flex-col gap-1"><span className="text-ghost">Built-up area ({form.area_unit})</span>
            <input value={form.built_up_area} onChange={e => set({ built_up_area: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>

          <label className="flex flex-col gap-1"><span className="text-ghost">Property no. (as per government)</span>
            <input value={form.property_no} onChange={e => set({ property_no: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
          <label className="flex flex-col gap-1"><span className="text-ghost">Survey no.</span>
            <input value={form.survey_no} onChange={e => set({ survey_no: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>

          <label className="flex flex-col gap-1 sm:col-span-2"><span className="text-ghost">GPS / Maps link <span className="normal-case">(leave blank to auto-derive from address)</span></span>
            <input value={form.gps_link} onChange={e => set({ gps_link: e.target.value })} placeholder="https://maps.google.com/…" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>

          <label className="flex flex-col gap-1"><span className="text-ghost">Date of acquisition</span>
            <input value={form.acquisition_date} type="date" onChange={e => set({ acquisition_date: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
          <label className="flex flex-col gap-1"><span className="text-ghost">Ownership (free text)</span>
            <input value={form.ownership} onChange={e => set({ ownership: e.target.value })} placeholder="e.g. Sole, Joint 50%" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>

          <div className="flex flex-col gap-1"><span className="text-ghost">Car parking</span>
            <div className="flex items-center gap-2">
              <label className="flex items-center gap-1.5 text-dim"><input type="checkbox" checked={form.has_parking} onChange={e => set({ has_parking: e.target.checked })} /> Yes</label>
              {form.has_parking && <input value={form.parking_count} onChange={e => set({ parking_count: e.target.value })} type="number" min="0" placeholder="How many" className="bg-page border border-rule rounded px-2 py-1.5 text-ink w-28" />}
            </div>
          </div>

          <label className="flex flex-col gap-1"><span className="text-ghost">Purchase price (₹)</span>
            <input value={form.purchase_price} onChange={e => set({ purchase_price: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
          <label className="flex flex-col gap-1"><span className="text-ghost">RRR (circle rate, ₹ per {form.area_unit})</span>
            <input value={form.rrr} onChange={e => set({ rrr: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
          <div className="flex flex-col gap-1"><span className="text-ghost">Land fair value ({fairMult}× RRR × area)</span>
            <span className="px-2.5 py-1.5 text-ink font-semibold tabular-nums">{fmtINR(formFair)}</span></div>
          <label className="flex flex-col gap-1"><span className="text-ghost">Market value (₹, optional)</span>
            <input value={form.market_value} onChange={e => set({ market_value: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>

          {/* Seller */}
          <div className="sm:col-span-2 border-t border-rule pt-3 mt-1">
            <p className="text-[11px] uppercase tracking-wide text-ghost mb-2">Purchased from a seller (optional)</p>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <label className="flex flex-col gap-1"><span className="text-ghost">Seller name</span>
                <input value={form.seller_name} onChange={e => set({ seller_name: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
              <label className="flex flex-col gap-1"><span className="text-ghost">Seller address</span>
                <input value={form.seller_address} onChange={e => set({ seller_address: e.target.value })} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
              <label className="flex flex-col gap-1"><span className="text-ghost">Stamp value (₹)</span>
                <input value={form.stamp_value} onChange={e => set({ stamp_value: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
              <label className="flex flex-col gap-1"><span className="text-ghost">Lawyer fees (₹)</span>
                <input value={form.lawyer_fees} onChange={e => set({ lawyer_fees: e.target.value })} type="number" min="0" step="any" className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
            </div>
            <p className="text-[11px] text-ghost mt-1.5">The agreement / sale deed itself uploads under Documents below (Seller Agreement Deed / Gift Deed).</p>
          </div>

          {/* Floors */}
          {buildingLike && (
            <div className="flex flex-col gap-1 sm:col-span-2 border-t border-rule pt-3">
              <span className="text-ghost">Floors — label, areas, rate &amp; tenancy</span>
              {form.floors.map((f, i) => {
                const floorVal = f.rate_per_unit && (f.built_up_area || f.area)
                  ? Number(f.built_up_area || f.area) * Number(f.rate_per_unit) : null;
                return (
                  <div key={i} className="border border-rule rounded p-2 flex flex-col gap-1.5">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <input value={f.floor_label} placeholder="e.g. Ground floor"
                             onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, floor_label: e.target.value } : x) })}
                             className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink flex-1 min-w-[9rem]" />
                      <button onClick={() => set({ floors: form.floors.filter((_, j) => j !== i) })} aria-label="Remove floor" className="text-ghost hover:text-red-500">✕</button>
                    </div>
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
                      <input value={f.area} type="number" min="0" step="any" placeholder="Area" aria-label="Floor area"
                             onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, area: e.target.value } : x) })}
                             className="bg-page border border-rule rounded px-2 py-1.5 text-ink" />
                      <input value={f.built_up_area} type="number" min="0" step="any" placeholder="Built-up" aria-label="Floor built-up"
                             onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, built_up_area: e.target.value } : x) })}
                             className="bg-page border border-rule rounded px-2 py-1.5 text-ink" />
                      <input value={f.carpet_area} type="number" min="0" step="any" placeholder="Carpet" aria-label="Floor carpet"
                             onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, carpet_area: e.target.value } : x) })}
                             className="bg-page border border-rule rounded px-2 py-1.5 text-ink" />
                      <input value={f.rate_per_unit} type="number" min="0" step="any" placeholder="Rate/unit" aria-label="Rate per unit"
                             onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, rate_per_unit: e.target.value } : x) })}
                             className="bg-page border border-rule rounded px-2 py-1.5 text-ink" />
                    </div>
                    <div className="flex flex-wrap items-center gap-2 text-ghost">
                      {floorVal != null && <span>Value {fmtINR(floorVal)}</span>}
                      <label className="flex items-center gap-1.5 text-dim"><input type="checkbox" checked={f.is_rented}
                        onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, is_rented: e.target.checked } : x) })} /> Rented / leased</label>
                      {f.is_rented && <>
                        <input value={f.rent_amount} type="number" min="0" step="any" placeholder="Rent ₹/mo" aria-label="Rent"
                               onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, rent_amount: e.target.value } : x) })}
                               className="bg-page border border-rule rounded px-2 py-1.5 text-ink w-28" />
                        <input value={f.tenant} placeholder="Tenant" aria-label="Tenant"
                               onChange={e => set({ floors: form.floors.map((x, j) => j === i ? { ...x, tenant: e.target.value } : x) })}
                               className="bg-page border border-rule rounded px-2 py-1.5 text-ink w-36" />
                        {form.id != null && f.id != null && (
                          <span className="flex gap-1">
                            <button onClick={() => onUpload('rent_agreement', f.id!)} className="text-[10px] border border-wire text-dim px-1.5 py-0.5 rounded hover:text-ink">+ rent agreement</button>
                            <button onClick={() => onUpload('lease_agreement', f.id!)} className="text-[10px] border border-wire text-dim px-1.5 py-0.5 rounded hover:text-ink">+ lease agreement</button>
                          </span>
                        )}
                      </>}
                    </div>
                  </div>
                );
              })}
              <button onClick={() => set({ floors: [...form.floors, { id: null, floor_label: '', area: '', rate_per_unit: '', built_up_area: '', carpet_area: '', is_rented: false, rent_amount: '', tenant: '' }] })}
                      className="self-start text-[11px] border border-wire text-dim px-2 py-0.5 rounded hover:border-dim hover:text-ink">+ Add floor</button>
              {form.id == null && form.floors.some(f => f.is_rented) && <p className="text-[11px] text-ghost">Tenancy agreements can be uploaded after saving.</p>}
            </div>
          )}

          <label className="flex flex-col gap-1 sm:col-span-2"><span className="text-ghost">Description</span>
            <textarea value={form.notes} onChange={e => set({ notes: e.target.value })} rows={2} className="bg-page border border-rule rounded px-2.5 py-1.5 text-ink" /></label>
        </div>

        {/* Photos + documents (post-save) */}
        {form.id == null ? (
          <div className="mt-5 border-t border-rule pt-4">
            <p className="text-xs text-ghost">Save the property first — photo upload and the {form.property_type} document checklist appear here right after.</p>
          </div>
        ) : (
          <div className="mt-5 border-t border-rule pt-4 flex flex-col gap-4">
            <div>
              <div className="flex items-center justify-between mb-2">
                <p className="text-[11px] uppercase tracking-wide text-ghost">Photos</p>
                {isAdmin && <button onClick={onUploadImage} disabled={busy === `img-${form.id}`} className="text-[11px] border border-wire text-dim px-2 py-0.5 rounded hover:border-dim hover:text-ink disabled:opacity-50">{busy === `img-${form.id}` ? 'Uploading…' : '+ Add photos'}</button>}
              </div>
              <p className="text-[11px] text-ghost">Manage the cover &amp; gallery from the property card. Upload adds to the gallery (first photo becomes the cover).</p>
            </div>
            <div>
              <p className="text-[11px] uppercase tracking-wide text-ghost mb-2">Documents — {form.property_type} checklist</p>
              <div className="max-h-72 overflow-y-auto pr-1">
                <FormDocChecklist types={docTypesFor(form.property_type)} docs={dataDocs(form.id)} isAdmin={isAdmin}
                  busy={busy === `upload-${form.id}`}
                  onUpload={slug => onUpload(slug)} onDeleteDoc={onDeleteDoc} />
              </div>
            </div>
          </div>
        )}

        {formErr && <p role="alert" className="text-xs text-red-500 mt-3">{formErr}</p>}
        {formSaved && !formErr && <p className="text-xs text-emerald-500 mt-3">Saved. You can add photos &amp; documents above.</p>}
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="text-xs border border-wire text-dim px-3 py-1.5 rounded hover:border-dim hover:text-ink">{formSaved ? 'Done' : 'Cancel'}</button>
          <button onClick={onSave} disabled={busy === 'save'} className="text-xs bg-prime text-prime-fg px-4 py-1.5 rounded font-medium hover:opacity-90 disabled:opacity-50">
            {busy === 'save' ? 'Saving…' : form.id == null ? 'Save & Continue' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  );
}

// Checklist inside the form (class-labelled links).
function FormDocChecklist({ types, docs, isAdmin, busy, onUpload, onDeleteDoc }: {
  types: DocType[]; docs: PropDoc[]; isAdmin: boolean; busy: boolean;
  onUpload: (slug: string) => void; onDeleteDoc: (d: PropDoc) => void;
}) {
  const byType: Record<string, PropDoc[]> = {};
  for (const d of docs) (byType[d.doc_type] ??= []).push(d);
  const topLevel = types.filter(t => !t.parent);
  const children = (slug: string) => types.filter(t => t.parent === slug);

  const row = (t: DocType, depth: number) => {
    const list = byType[t.slug] ?? [];
    const has = list.length > 0;
    return (
      <li key={t.slug} className={`flex items-start gap-2 py-1 ${depth ? 'pl-5' : ''}`}>
        <span className="mt-0.5">{has ? <span className="text-emerald-500">●</span> : <span className={t.optional ? 'text-ghost' : 'text-red-500'}>○</span>}</span>
        <div className="min-w-0 flex-1">
          <span className={has ? 'text-ink' : 'text-dim'}>{t.label}{t.optional && <span className="text-ghost"> (if any)</span>}</span>
          {list.map(d => (
            <span key={d.id} className="ml-2 inline-flex items-center gap-1">
              <a href={docUrl(d.id)} target="_blank" rel="noopener noreferrer" className="text-prime hover:underline">
                {docDisplayName(d, list, t.label)}{d.converted ? ' (PDF)' : ''}
              </a>
              {d.has_original && <a href={docUrl(d.id, true)} target="_blank" rel="noopener noreferrer" className="text-[10px] text-ghost hover:text-ink" title="Original upload">orig</a>}
              {isAdmin && <button onClick={() => onDeleteDoc(d)} className="text-ghost hover:text-red-500 text-[10px]" aria-label="Delete document">✕</button>}
            </span>
          ))}
        </div>
        {isAdmin && <button onClick={() => onUpload(t.slug)} disabled={busy} className="shrink-0 text-[10px] border border-wire text-dim px-1.5 py-0.5 rounded hover:border-dim hover:text-ink disabled:opacity-50">Upload</button>}
      </li>
    );
  };

  return (
    <ul className="columns-1 sm:columns-2 gap-8 text-xs [&>li]:break-inside-avoid">
      {topLevel.map(t => (
        <li key={t.slug} className="mb-0.5"><ul>{row(t, 0)}{children(t.slug).map(c => row(c, 1))}</ul></li>
      ))}
    </ul>
  );
}
