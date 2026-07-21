'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

interface Me { id: number; email: string; full_name?: string; role: string; }
interface UserRow { email: string; full_name?: string; role: string; }
interface ResetReq { id: number; email: string; full_name?: string; requested_at?: string | null; }

export default function AccountPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);
  const [checking, setChecking] = useState(true);

  // change password
  const [cur, setCur] = useState('');
  const [nw, setNw] = useState('');
  const [confirm, setConfirm] = useState('');
  const [cpMsg, setCpMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [cpBusy, setCpBusy] = useState(false);

  // admin reset
  const [users, setUsers] = useState<UserRow[]>([]);
  const [requests, setRequests] = useState<ResetReq[]>([]);
  const [rEmail, setREmail] = useState('');
  const [rPw, setRPw] = useState('');
  const [rMsg, setRMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [rBusy, setRBusy] = useState(false);

  useEffect(() => {
    const c = new AbortController();
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include', signal: c.signal })
      .then(r => r.ok ? r.json() : null)
      .then((u: Me | null) => { if (!u) { router.replace('/'); return; } setMe(u); setChecking(false); })
      .catch(err => { if (err.name !== 'AbortError') router.replace('/'); });
    return () => c.abort();
  }, [router]);

  const loadAdmin = useCallback(() => {
    fetch(`${API_URL}/api/v1/auth/users`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { users: [] }).then(d => setUsers(d.users ?? [])).catch(() => {});
    fetch(`${API_URL}/api/v1/auth/reset-requests`, { credentials: 'include' })
      .then(r => r.ok ? r.json() : { requests: [] }).then(d => setRequests(d.requests ?? [])).catch(() => {});
  }, []);

  useEffect(() => { if (me?.role === 'admin') loadAdmin(); }, [me, loadAdmin]);

  const submitChange = async (e: { preventDefault(): void }) => {
    e.preventDefault();
    setCpMsg(null);
    if (nw !== confirm) { setCpMsg({ ok: false, text: 'New password and confirmation do not match.' }); return; }
    setCpBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/auth/change-password`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: cur, new_password: nw }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { setCpMsg({ ok: false, text: d.detail || 'Could not change password.' }); return; }
      setCpMsg({ ok: true, text: 'Password changed successfully.' });
      setCur(''); setNw(''); setConfirm('');
    } catch {
      setCpMsg({ ok: false, text: 'Network error. Please try again.' });
    } finally { setCpBusy(false); }
  };

  const submitReset = async (e: { preventDefault(): void }) => {
    e.preventDefault();
    setRMsg(null); setRBusy(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/auth/admin-reset-password`, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: rEmail, new_password: rPw }),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok) { setRMsg({ ok: false, text: d.detail || 'Could not reset password.' }); return; }
      setRMsg({ ok: true, text: d.message || 'Password reset.' });
      setRPw(''); loadAdmin();
    } catch {
      setRMsg({ ok: false, text: 'Network error. Please try again.' });
    } finally { setRBusy(false); }
  };

  const inputCls = 'w-full border border-wire rounded-md px-3 py-2 text-base text-ink bg-card hover:border-dim focus:outline-none focus:ring-2 focus:ring-prime disabled:opacity-60 transition-[border-color,box-shadow] duration-150';

  if (checking) return (
    <main id="main-content" className="min-h-screen flex items-center justify-center bg-page">
      <div aria-hidden="true" className="w-5 h-5 rounded-full border-2 border-rule border-t-prime animate-spin" />
    </main>
  );

  return (
    <main id="main-content" className="min-h-screen bg-page py-4 sm:py-8">
      <div className="shell">
        <div className="flex flex-wrap justify-between items-start gap-3 mb-6">
          <div>
            <h1 className="text-2xl sm:text-3xl font-bold text-ink">Account</h1>
            <p className="text-sm text-ghost mt-0.5">Signed in as {me?.email} · {me?.role}</p>
          </div>
        </div>

        <div className="grid gap-6 md:grid-cols-2 max-w-4xl">
          {/* Change password — all users */}
          <section className="bg-card rounded-lg border border-rule p-5 sm:p-6">
            <h2 className="text-lg font-semibold text-ink mb-4">Change password</h2>
            <form onSubmit={submitChange} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-dim mb-1" htmlFor="cur">Current password</label>
                <input id="cur" type="password" autoComplete="current-password" required maxLength={72}
                  value={cur} onChange={e => setCur(e.target.value)} disabled={cpBusy} className={inputCls} />
              </div>
              <div>
                <label className="block text-sm font-medium text-dim mb-1" htmlFor="nw">New password</label>
                <input id="nw" type="password" autoComplete="new-password" required minLength={8} maxLength={72}
                  value={nw} onChange={e => setNw(e.target.value)} disabled={cpBusy} className={inputCls} />
                <p className="text-[11px] text-ghost mt-1">8–72 characters, with an uppercase letter, a lowercase letter, and a digit.</p>
              </div>
              <div>
                <label className="block text-sm font-medium text-dim mb-1" htmlFor="cf">Confirm new password</label>
                <input id="cf" type="password" autoComplete="new-password" required minLength={8} maxLength={72}
                  value={confirm} onChange={e => setConfirm(e.target.value)} disabled={cpBusy} className={inputCls} />
              </div>
              {cpMsg && (
                <p role="alert" className={`text-sm ${cpMsg.ok ? 'text-gain' : 'text-peril'}`}>{cpMsg.text}</p>
              )}
              <button type="submit" disabled={cpBusy} aria-busy={cpBusy}
                className="w-full bg-prime text-prime-fg py-2.5 rounded-md text-sm font-medium hover:bg-prime-deep disabled:opacity-60 transition-colors duration-150">
                {cpBusy ? 'Saving...' : 'Change password'}
              </button>
            </form>
          </section>

          {/* Admin: reset a user's password */}
          {me?.role === 'admin' && (
            <section className="bg-card rounded-lg border border-rule p-5 sm:p-6">
              <h2 className="text-lg font-semibold text-ink mb-4">Reset a user&apos;s password</h2>

              {requests.length > 0 && (
                <div className="mb-4">
                  <p className="text-xs font-medium text-ghost mb-2">Pending reset requests</p>
                  <ul className="space-y-1.5">
                    {requests.map(r => (
                      <li key={r.id} className="flex items-center justify-between gap-2 text-sm border border-rule rounded px-2.5 py-1.5">
                        <span className="text-dim truncate">{r.email}{r.full_name ? ` · ${r.full_name}` : ''}</span>
                        <button type="button" onClick={() => setREmail(r.email)}
                          className="shrink-0 text-xs border border-wire text-dim px-2 py-0.5 rounded hover:border-dim hover:text-ink transition-colors">
                          Select
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <form onSubmit={submitReset} className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-dim mb-1" htmlFor="ru">User</label>
                  <select id="ru" required value={rEmail} onChange={e => setREmail(e.target.value)} disabled={rBusy} className={inputCls}>
                    <option value="" disabled>Select a user…</option>
                    {users.map(u => (
                      <option key={u.email} value={u.email}>{u.email}{u.full_name ? ` — ${u.full_name}` : ''} ({u.role})</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-dim mb-1" htmlFor="rpw">New password</label>
                  <input id="rpw" type="text" autoComplete="off" required minLength={8} maxLength={72}
                    value={rPw} onChange={e => setRPw(e.target.value)} disabled={rBusy} className={inputCls} />
                  <p className="text-[11px] text-ghost mt-1">Share this with the user; they can change it from this page after signing in.</p>
                </div>
                {rMsg && (
                  <p role="alert" className={`text-sm ${rMsg.ok ? 'text-gain' : 'text-peril'}`}>{rMsg.text}</p>
                )}
                <button type="submit" disabled={rBusy} aria-busy={rBusy}
                  className="w-full bg-prime text-prime-fg py-2.5 rounded-md text-sm font-medium hover:bg-prime-deep disabled:opacity-60 transition-colors duration-150">
                  {rBusy ? 'Resetting...' : 'Reset password'}
                </button>
              </form>
            </section>
          )}
        </div>

        <p className="text-center text-xs text-ghost mt-8">Rajani MIS &copy; {new Date().getFullYear()}</p>
      </div>
    </main>
  );
}
