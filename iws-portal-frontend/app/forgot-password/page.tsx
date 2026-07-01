'use client';
import { useState } from 'react';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: { preventDefault(): void }) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const res = await fetch(`${API_URL}/api/v1/auth/forgot-password`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      });
      await res.json();
      // The API is deliberately generic (no account enumeration), so any 2xx is "done".
      if (!res.ok && res.status !== 429) throw new Error();
      if (res.status === 429) { setError('Too many requests. Please try again in a minute.'); return; }
      setDone(true);
    } catch {
      setError('Network error. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main id="main-content" className="min-h-screen flex flex-col items-center justify-center bg-page px-4 py-10">
      <div className="bg-card p-6 sm:p-8 rounded-lg shadow-sm border border-rule w-full max-w-sm">
        <p className="text-center text-xs font-semibold text-ghost tracking-widest uppercase mb-2">
          IWS Finserv
        </p>
        <h1 className="text-2xl font-bold text-ink mb-2 text-center">Reset password</h1>

        {done ? (
          <>
            <p className="text-sm text-dim text-center mt-4">
              If an account exists for that email, your administrator has been notified and will
              reset the password. Please contact your administrator to receive the new password.
            </p>
            <p className="text-center mt-6">
              <a href="/" className="text-xs text-dim hover:text-ink underline underline-offset-2 transition-colors">
                Back to sign in
              </a>
            </p>
          </>
        ) : (
          <>
            <p className="text-sm text-ghost mb-6 text-center">
              Enter your account email and your administrator will be notified to reset it.
            </p>
            <form onSubmit={handleSubmit} className="space-y-4" aria-describedby={error ? 'forgot-error' : undefined}>
              <div>
                <label className="block text-sm font-medium text-dim mb-1" htmlFor="email">Email</label>
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
              {error && (
                <p id="forgot-error" role="alert" className="error-enter text-peril text-sm">{error}</p>
              )}
              <button
                type="submit"
                disabled={submitting}
                aria-busy={submitting}
                className="w-full bg-prime text-prime-fg py-3 rounded-md text-sm font-medium hover:bg-prime-deep disabled:opacity-60 disabled:cursor-not-allowed transition-colors duration-150"
              >
                {submitting ? 'Submitting...' : 'Request reset'}
              </button>
            </form>
            <p className="text-center mt-6">
              <a href="/" className="text-xs text-dim hover:text-ink underline underline-offset-2 transition-colors">
                Back to sign in
              </a>
            </p>
          </>
        )}
      </div>
    </main>
  );
}
