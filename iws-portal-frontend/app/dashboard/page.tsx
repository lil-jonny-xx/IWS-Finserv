'use client';
import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';
const IDLE_TIMEOUT = 30 * 60 * 1000;

export default function DashboardPage() {
  const [user, setUser] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const router = useRouter();

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await fetch(`${API_URL}/api/v1/auth/logout`, {
        method: 'POST',
        credentials: 'include',
      });
    } catch (err) {
      console.error('Logout error:', err);
    } finally {
      router.push('/');
    }
  }, [router]);

  useEffect(() => {
    let idleTimer: NodeJS.Timeout;
    const resetTimer = () => {
      clearTimeout(idleTimer);
      idleTimer = setTimeout(handleLogout, IDLE_TIMEOUT);
    };
    const events = ['mousedown', 'keypress', 'scroll', 'touchstart'];
    events.forEach(e => window.addEventListener(e, resetTimer));
    resetTimer();
    return () => {
      clearTimeout(idleTimer);
      events.forEach(e => window.removeEventListener(e, resetTimer));
    };
  }, [handleLogout]);

  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, {
      credentials: 'include',
    })
      .then(res => {
        if (!res.ok) throw new Error('Session expired or invalid');
        return res.json();
      })
      .then(data => setUser(data))
      .catch(err => {
        setError(err.message);
        setTimeout(() => router.push('/'), 3000);
      });
  }, [router]);

  if (error) return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100">
      <div className="bg-white p-8 rounded-lg shadow-md text-center">
        <p className="text-red-600 font-medium mb-2">Session Error</p>
        <p className="text-gray-600 text-sm">{error}</p>
        <p className="text-gray-400 text-xs mt-2">Redirecting to login...</p>
      </div>
    </div>
  );

  if (!user) return (
    <div className="min-h-screen flex items-center justify-center bg-gray-100">
      <div className="bg-white p-8 rounded-lg shadow-md text-center">
        <p className="text-gray-600">Loading your dashboard...</p>
      </div>
    </div>
  );

  return (
    <div className="min-h-screen bg-gray-100 p-8">
      <div className="max-w-4xl mx-auto">
        <div className="flex justify-between items-center mb-6">
          <h1 className="text-3xl font-bold text-gray-800">
            Welcome, {user.full_name}!
          </h1>
          <button
            onClick={handleLogout}
            disabled={loggingOut}
            className="bg-red-600 text-white px-4 py-2 rounded-lg hover:bg-red-700 disabled:bg-gray-400 transition-colors text-sm"
          >
            {loggingOut ? 'Signing out...' : 'Sign Out'}
          </button>
        </div>

        <div className="bg-white p-6 rounded-lg shadow-md mb-4">
          <h2 className="text-lg font-semibold text-gray-700 mb-4">Your Profile</h2>
          <div className="space-y-2 text-sm">
            <p><span className="font-medium text-gray-600">Email:</span> {user.email}</p>
            <p><span className="font-medium text-gray-600">Role:</span> {user.role}</p>
            <p><span className="font-medium text-gray-600">Entity ID:</span> {user.entity_id}</p>
          </div>
        </div>

        <div className="bg-blue-50 border border-blue-200 p-4 rounded-lg">
          <p className="text-blue-700 text-sm font-medium">Portfolio dashboard coming soon.</p>
          <p className="text-blue-500 text-xs mt-1">Data ingestion workers are being configured.</p>
        </div>

        <p className="text-center text-xs text-gray-400 mt-8">
          IWS Finserv &copy; {new Date().getFullYear()} · Session expires after 30 mins of inactivity
        </p>
      </div>
    </div>
  );
}
