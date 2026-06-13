// Message input. Enter sends; Shift+Enter inserts a newline. Disabled while streaming.
import { useEffect, useRef, useState } from 'react';

export default function Composer({
  onSend,
  disabled,
  busy,
}: {
  onSend: (text: string) => void;
  disabled: boolean; // no active conversation
  busy: boolean;     // a turn is streaming
}) {
  const [text, setText] = useState('');
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-grow the textarea up to a cap.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
  }, [text]);

  function submit() {
    const t = text.trim();
    if (!t || busy || disabled) return;
    onSend(t.slice(0, 4000));
    setText('');
  }

  return (
    <div className="border-t border-rule bg-card px-4 py-3 sm:px-6">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <textarea
          ref={taRef}
          value={text}
          onChange={e => setText(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={1}
          maxLength={4000}
          disabled={disabled}
          placeholder={
            disabled
              ? 'Start or open a conversation to begin…'
              : 'Ask about allocation, performance, or a potential investment…'
          }
          className="max-h-52 flex-1 resize-none rounded-lg border border-wire bg-page px-3.5 py-2.5 text-sm text-ink placeholder:text-ghost focus:border-prime focus:outline-none focus:ring-1 focus:ring-prime disabled:opacity-60"
        />
        <button
          type="button"
          onClick={submit}
          disabled={disabled || busy || !text.trim()}
          aria-label="Send message"
          className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-prime text-prime-fg transition-colors hover:bg-prime-deep disabled:cursor-not-allowed disabled:opacity-40"
        >
          {busy ? (
            <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-prime-fg/40 border-t-prime-fg" />
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M22 2 11 13" />
              <path d="M22 2 15 22l-4-9-9-4 20-7Z" />
            </svg>
          )}
        </button>
      </div>
      <p className="mx-auto mt-1.5 max-w-3xl px-1 text-[11px] text-ghost">
        Jarvis is read-only — it analyses your portfolio but never places or changes orders.
      </p>
    </div>
  );
}
