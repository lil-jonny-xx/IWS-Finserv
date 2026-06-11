// Renders the message list plus the live streaming assistant bubble.
import { useEffect, useRef } from 'react';
import type { Citation, Message } from '../types';
import Citations from './Citations';

export interface StreamingState {
  active: boolean;          // a turn is in flight
  text: string;             // accumulated assistant deltas
  tool: string | null;      // tool currently running (affordance)
  citations: Citation[];    // staged citation chips
  error: string | null;     // clean error event (e.g. API key pending)
}

const TOOL_LABELS: Record<string, string> = {
  web_search: 'Searching the web',
};

function toolLabel(name: string): string {
  return TOOL_LABELS[name] || `Running ${name.replace(/_/g, ' ')}`;
}

function Bubble({ role, children }: { role: 'user' | 'assistant'; children: React.ReactNode }) {
  if (role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-prime px-4 py-2.5 text-sm leading-relaxed text-prime-fg">
          {children}
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-[90%] text-sm leading-relaxed text-ink">{children}</div>
    </div>
  );
}

export default function ChatThread({
  messages,
  streaming,
}: {
  messages: Message[];
  streaming: StreamingState;
}) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [messages, streaming.text, streaming.tool, streaming.active]);

  const empty = messages.length === 0 && !streaming.active;

  return (
    <div className="flex-1 overflow-y-auto px-4 py-6 sm:px-6">
      <div className="mx-auto max-w-3xl space-y-5">
        {empty && (
          <div className="mt-10 text-center">
            <h3 className="text-lg font-semibold text-ink">How can I help with the portfolio?</h3>
            <p className="mx-auto mt-2 max-w-md text-sm text-ghost">
              Ask about sector performance, asset allocation, what’s underperforming and why,
              or a fundamental view on a potential new investment.
            </p>
          </div>
        )}

        {messages.map(m => (
          <div key={m.id}>
            <Bubble role={m.role}>
              <span className="whitespace-pre-wrap">{m.content}</span>
            </Bubble>
            {m.role === 'assistant' && m.citations?.length ? (
              <div className="mt-1 flex justify-start">
                <div className="max-w-[90%]">
                  <Citations items={m.citations} />
                </div>
              </div>
            ) : null}
          </div>
        ))}

        {/* Live streaming assistant turn */}
        {streaming.active && (
          <div>
            <Bubble role="assistant">
              {streaming.tool && !streaming.text && (
                <span className="inline-flex items-center gap-2 rounded-full border border-rule bg-card px-3 py-1.5 text-xs text-dim">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-prime" />
                  {toolLabel(streaming.tool)}…
                </span>
              )}
              {streaming.text && (
                <span className="whitespace-pre-wrap">
                  {streaming.text}
                  <span className="ml-0.5 inline-block h-4 w-[2px] -translate-y-px animate-pulse bg-prime align-middle" />
                </span>
              )}
              {!streaming.tool && !streaming.text && (
                <span className="inline-flex items-center gap-1.5 text-sm text-ghost">
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ghost [animation-delay:-0.3s]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ghost [animation-delay:-0.15s]" />
                  <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-ghost" />
                </span>
              )}
            </Bubble>
            {streaming.citations.length > 0 && (
              <div className="mt-1 flex justify-start">
                <div className="max-w-[90%]">
                  <Citations items={streaming.citations} />
                </div>
              </div>
            )}
          </div>
        )}

        {/* Clean error surface (e.g. ANTHROPIC_API_KEY pending) */}
        {streaming.error && (
          <div className="flex justify-start">
            <div className="max-w-[90%] rounded-lg border border-peril/30 bg-peril/5 px-4 py-2.5 text-sm text-peril">
              {streaming.error}
            </div>
          </div>
        )}

        <div ref={endRef} />
      </div>
    </div>
  );
}
