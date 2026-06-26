'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import type {
  Citation,
  ChartSpec,
  Conversation,
  ConversationDetail,
  Entity,
  Message,
  User,
} from './types';
import { streamChat } from './lib/stream';
import ConversationSidebar from './components/ConversationSidebar';
import ChatThread, { type StreamingState } from './components/ChatThread';
import Composer from './components/Composer';
import ScopeSelector from './components/ScopeSelector';
import ContextCard from './components/ContextCard';

const API_URL = process.env.NEXT_PUBLIC_API_URL || 'https://iwsfinserv.com';

const NAV = [
  { href: '/dashboard',    label: 'Overview'     },
  { href: '/mutual-funds', label: 'Mutual Funds' },
  { href: '/equity',       label: 'Equity'       },
  { href: '/foreign-equity', label: 'Foreign Equity' },
  { href: '/gold-silver', label: 'Gold/Silver' },
  { href: '/unlisted', label: 'Unlisted' },
  { href: '/art', label: 'Art' },
  { href: '/properties', label: 'Properties' },
  { href: '/bank-accounts', label: 'Banks' },
  { href: '/pms', label: 'PMS' },
  { href: '/manual-data',  label: 'Manual Data'  },
  { href: '/reports',      label: 'Reports'      },
  { href: '/benchmarks',   label: 'Benchmarks'   },
  { href: '/realised-gains', label: 'Realised Gains' },
  { href: '/assistant',    label: 'Assistant', active: true },
];

const EMPTY_STREAM: StreamingState = {
  active: false,
  text: '',
  tool: null,
  citations: [],
  charts: [],
  error: null,
};

export default function AssistantPage() {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [entities, setEntities] = useState<Entity[]>([]);

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [listLoading, setListLoading] = useState(true);

  const [activeId, setActiveId] = useState<number | null>(null);
  const [activeScope, setActiveScope] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);

  const [streaming, setStreaming] = useState<StreamingState>(EMPTY_STREAM);
  const [newScope, setNewScope] = useState<number | null>(null);
  const [showScopeModal, setShowScopeModal] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const isAdmin = user?.role === 'admin';

  // ── Auth + entities ──────────────────────────────
  useEffect(() => {
    fetch(`${API_URL}/api/v1/me`, { credentials: 'include' })
      .then(r => { if (r.status === 401) { router.push('/'); return null; } return r.json(); })
      .then((u: User | null) => {
        if (!u) return;
        setUser(u);
        if (u.role === 'admin') {
          fetch(`${API_URL}/api/v1/entities`, { credentials: 'include' })
            .then(r => (r.ok ? r.json() : []))
            .then((ents: Entity[]) => setEntities(ents))
            .catch(() => {});
        }
      })
      .catch(() => router.push('/'));
  }, [router]);

  // ── Conversation list ────────────────────────────
  const loadConversations = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/api/v1/assistant/conversations`, { credentials: 'include' });
      if (r.status === 401) { router.push('/'); return; }
      const d = await r.json();
      setConversations(d.conversations ?? []);
    } catch {
      /* leave list as-is */
    } finally {
      setListLoading(false);
    }
  }, [router]);

  useEffect(() => { loadConversations(); }, [loadConversations]);

  // Abort any in-flight stream on unmount.
  useEffect(() => () => abortRef.current?.abort(), []);

  // ── Open an existing conversation ────────────────
  const openConversation = useCallback(async (id: number) => {
    abortRef.current?.abort();
    setStreaming(EMPTY_STREAM);
    setActiveId(id);
    try {
      const r = await fetch(`${API_URL}/api/v1/assistant/conversations/${id}`, { credentials: 'include' });
      if (r.status === 401) { router.push('/'); return; }
      const d: ConversationDetail = await r.json();
      setMessages(d.messages ?? []);
      setActiveScope(d.scope_entity_id ?? null);
    } catch {
      setMessages([]);
    }
  }, [router]);

  // ── Create a conversation ────────────────────────
  const createConversation = useCallback(async (entityId: number | null) => {
    try {
      const r = await fetch(`${API_URL}/api/v1/assistant/conversations`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(entityId != null ? { entity_id: entityId } : {}),
      });
      if (r.status === 401) { router.push('/'); return; }
      if (!r.ok) return;
      const c: Conversation = await r.json();
      setConversations(prev => [c, ...prev]);
      setActiveId(c.id);
      setActiveScope(c.scope_entity_id ?? null);
      setMessages([]);
      setStreaming(EMPTY_STREAM);
    } catch {
      /* ignore */
    }
  }, [router]);

  const handleNew = useCallback(() => {
    if (isAdmin) {
      setNewScope(null);
      setShowScopeModal(true);
    } else {
      createConversation(null); // member: server pins scope
    }
  }, [isAdmin, createConversation]);

  // ── Archive ──────────────────────────────────────
  const handleArchive = useCallback(async (id: number) => {
    try {
      await fetch(`${API_URL}/api/v1/assistant/conversations/${id}/archive`, {
        method: 'POST',
        credentials: 'include',
      });
    } catch {
      /* still drop it locally */
    }
    setConversations(prev => prev.filter(c => c.id !== id));
    if (id === activeId) {
      abortRef.current?.abort();
      setActiveId(null);
      setMessages([]);
      setStreaming(EMPTY_STREAM);
    }
  }, [activeId]);

  // ── Send a message (stream the reply) ────────────
  const handleSend = useCallback(async (text: string) => {
    if (activeId == null || streaming.active) return;

    const userMsg: Message = {
      id: -Date.now(),
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);
    setStreaming({ ...EMPTY_STREAM, active: true });

    const controller = new AbortController();
    abortRef.current = controller;

    let acc = '';
    let cites: Citation[] = [];
    let chartsAcc: ChartSpec[] = [];

    try {
      for await (const ev of streamChat(activeId, text, controller.signal)) {
        if (ev.type === 'text') {
          acc += ev.text;
          setStreaming(s => ({ ...s, text: acc }));
        } else if (ev.type === 'tool') {
          setStreaming(s => ({ ...s, tool: ev.name }));
        } else if (ev.type === 'chart') {
          chartsAcc = [...chartsAcc, ev.spec];
          setStreaming(s => ({ ...s, charts: chartsAcc }));
        } else if (ev.type === 'citations') {
          cites = ev.items;
          setStreaming(s => ({ ...s, citations: ev.items }));
        } else if (ev.type === 'done') {
          const assistantMsg: Message = {
            id: -Date.now() - 1,
            role: 'assistant',
            content: ev.content || acc,
            citations: ev.citations?.length ? ev.citations : cites,
            charts: ev.charts?.length ? ev.charts : (chartsAcc.length ? chartsAcc : null),
            tool_calls: ev.tool_names ?? null,
            created_at: new Date().toISOString(),
          };
          setMessages(prev => [...prev, assistantMsg]);
          setStreaming(EMPTY_STREAM);
          loadConversations(); // refresh titles/ordering
        } else if (ev.type === 'error') {
          setStreaming({ ...EMPTY_STREAM, error: ev.message });
        }
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'AbortError')) {
        setStreaming({ ...EMPTY_STREAM, error: 'The connection was interrupted. Please try again.' });
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [activeId, streaming.active, loadConversations]);

  return (
    <main id="main-content" className="flex h-screen flex-col overflow-hidden bg-page">
      {/* Header */}
      <header className="shrink-0 border-b border-rule bg-card px-4 py-3 sm:px-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-baseline gap-2">
            <h1 className="text-xl font-bold text-ink">Assistant</h1>
            <span className="text-xs text-ghost">Read-only portfolio advisor</span>
          </div>
          <nav className="flex gap-1.5" aria-label="Sections">
            {NAV.map(({ href, label, active }) => (
              <a
                key={href}
                href={href}
                aria-current={active ? 'page' : undefined}
                className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
                  active
                    ? 'bg-prime text-prime-fg'
                    : 'border border-rule bg-card text-dim hover:border-dim hover:text-ink'
                }`}
              >
                {label}
              </a>
            ))}
          </nav>
        </div>
      </header>

      {/* Body */}
      <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
        <ConversationSidebar
          conversations={conversations}
          activeId={activeId}
          loading={listLoading}
          onNew={handleNew}
          onOpen={openConversation}
          onArchive={handleArchive}
        />

        <section className="flex min-h-0 flex-1 flex-col">
          {activeId == null ? (
            <div className="flex flex-1 items-center justify-center px-6 text-center">
              <div className="rise-in">
                <h2 className="text-lg font-semibold text-ink">Welcome to Jarvis</h2>
                <p className="mx-auto mt-2 max-w-md text-sm text-ghost">
                  Your read-only financial copilot. Start a new conversation to analyse
                  allocation, spot underperforming sectors, or weigh a potential investment.
                </p>
                <button
                  type="button"
                  onClick={handleNew}
                  className="mt-5 inline-flex items-center gap-1.5 rounded-md bg-prime px-4 py-2 text-sm font-medium text-prime-fg transition-colors hover:bg-prime-deep"
                >
                  New conversation
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="shrink-0 px-4 pt-4 sm:px-6">
                <ContextCard scopeEntityId={activeScope} entities={entities} isAdmin={isAdmin} />
              </div>
              <ChatThread messages={messages} streaming={streaming} />
              <Composer onSend={handleSend} disabled={false} busy={streaming.active} />
            </>
          )}
        </section>
      </div>

      {/* Admin: choose scope when starting a conversation */}
      {showScopeModal && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
          role="dialog"
          aria-modal="true"
          aria-label="New conversation scope"
          onClick={() => setShowScopeModal(false)}
        >
          <div
            className="w-full max-w-md rounded-xl border border-rule bg-card p-5 shadow-xl"
            onClick={e => e.stopPropagation()}
          >
            <h3 className="text-base font-semibold text-ink">New conversation</h3>
            <p className="mt-1 mb-4 text-xs text-ghost">
              Choose the portfolio Jarvis should advise on. This is fixed for the conversation.
            </p>
            <ScopeSelector entities={entities} value={newScope} onChange={setNewScope} />
            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setShowScopeModal(false)}
                className="rounded-md border border-wire px-3.5 py-2 text-sm text-dim transition-colors hover:border-dim hover:text-ink"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => { setShowScopeModal(false); createConversation(newScope); }}
                className="rounded-md bg-prime px-4 py-2 text-sm font-medium text-prime-fg transition-colors hover:bg-prime-deep"
              >
                Start
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
