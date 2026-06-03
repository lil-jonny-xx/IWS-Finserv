'use client';
import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

export default function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const router = useRouter();

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include', signal: controller.signal })
      .then(res => {
        if (res.ok) router.replace('/dashboard');
        else setChecking(false);
      })
      .catch(err => {
        if (err.name !== 'AbortError') setChecking(false);
      });
    return () => controller.abort();
  }, [router]);

  const handleSubmit = async (e: { preventDefault(): void }) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/auth/login`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError('Invalid credentials or account locked. Please try again.');
        return;
      }
      router.push('/dashboard');
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  if (checking) return (
    <main id="main-content" className="min-h-screen flex items-center justify-center bg-page">
      <span role="status" aria-live="polite" className="sr-only">Checking session</span>
      <div
        aria-hidden="true"
        className="w-5 h-5 rounded-full border-2 border-rule border-t-prime animate-spin"
      />
    </main>
  );

  return (
    <main id="main-content" className="min-h-screen flex flex-col items-center justify-center bg-page px-4 py-10">
      <div className="bg-card p-6 sm:p-8 rounded-lg shadow-sm border border-rule w-full max-w-sm">
        <p className="text-center text-xs font-semibold text-ghost tracking-widest uppercase mb-2">
          IWS Finserv
        </p>
        <h1 className="text-2xl font-bold text-ink mb-6 text-center">Sign in</h1>
        <form
          onSubmit={handleSubmit}
          className="space-y-4"
          aria-describedby={error ? 'login-error' : undefined}
        >
          <div>
            <label className="block text-sm font-medium text-dim mb-1" htmlFor="email">
              Email
            </label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              maxLength={254}
              disabled={submitting}
              value={email}
              onChange={e => setEmail(e.target.value)}
              className="w-full border border-wire rounded-md px-3 py-2 text-base text-ink bg-card hover:border-dim focus:outline-none focus:ring-2 focus:ring-prime disabled:opacity-60 disabled:cursor-not-allowed transition-[border-color,box-shadow] duration-150"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-dim mb-1" htmlFor="password">
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
              maxLength={72}
              disabled={submitting}
              value={password}
              onChange={e => setPassword(e.target.value)}
              className="w-full border border-wire rounded-md px-3 py-2 text-base text-ink bg-card hover:border-dim focus:outline-none focus:ring-2 focus:ring-prime disabled:opacity-60 disabled:cursor-not-allowed transition-[border-color,box-shadow] duration-150"
            />
          </div>
          {error && (
            <p id="login-error" role="alert" className="text-peril text-sm">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={submitting}
            aria-busy={submitting}
            className="w-full bg-prime text-prime-fg py-3 rounded-md text-sm font-medium hover:bg-prime-deep disabled:opacity-60 disabled:cursor-not-allowed transition-colors duration-150"
          >
            {submitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
        <p className="text-center text-xs text-ghost mt-6">
          IWS Finserv &copy; {new Date().getFullYear()}
        </p>
      </div>
    </main>
  );
}
