'use client';
// Privacy glass — a frosted pane over the headline totals at the top of each page.
//
// Deliberately in-memory only. Nothing is written to localStorage or
// sessionStorage, so a reveal cannot outlive the document that made it: open the
// laptop tomorrow and every figure is covered again, with no state to clear.
//
// Note what that means here specifically. NavTabs renders plain <a>, not
// next/link (see the note in lib/useMe.ts), so moving between sections is a full
// document load — which re-glasses. That is the intended reading of "temporary",
// not an oversight: the reveal lasts as long as the page you asked it on.
//
// Scope is the top summary strip only. Per-row figures and the bottom totals bar
// stay legible; the aim is to stop a passer-by reading the headline number, not
// to make the page unusable.
//
// The figures remain in the DOM and in the accessibility tree. This defends
// against someone reading your screen, not against someone reading your markup —
// blurring is a visual affordance, never an access control. Anything that must
// actually be withheld has to be withheld by the API.
import {
  createContext, useCallback, useContext, useMemo, useState, type ReactNode,
} from 'react';

interface PrivacyState {
  revealed: boolean;
  toggle: () => void;
  reveal: () => void;
}

// Default is a no-op provider-less state so a stray <Glass> outside the provider
// renders covered rather than throwing — failing closed is the safe direction.
const PrivacyContext = createContext<PrivacyState>({
  revealed: false,
  toggle: () => {},
  reveal: () => {},
});

export function PrivacyProvider({ children }: { children: ReactNode }) {
  const [revealed, setRevealed] = useState(false);
  const toggle = useCallback(() => setRevealed(v => !v), []);
  const reveal = useCallback(() => setRevealed(true), []);
  const value = useMemo(() => ({ revealed, toggle, reveal }), [revealed, toggle, reveal]);
  return <PrivacyContext.Provider value={value}>{children}</PrivacyContext.Provider>;
}

export function usePrivacy() {
  return useContext(PrivacyContext);
}

/**
 * Wraps a summary block in a frosted pane. Click anywhere on the pane to reveal;
 * the eye control in the top bar toggles every pane on the page at once.
 *
 * `label` completes the phrase "Show <label>" — both the visible hint and the
 * button's accessible name — so it should read as a plural noun ("totals",
 * "portfolio totals").
 */
export function Glass({ children, label = 'totals', className = '' }: {
  children: ReactNode;
  label?: string;
  className?: string;
}) {
  const { revealed, reveal } = usePrivacy();
  return (
    // `isolate` keeps the pane's z-index from escaping into whatever the page
    // stacks around this block; rounding is inherited by .privacy-glass so the
    // pane follows the card's own corners.
    <div className={`relative isolate rounded-lg ${className}`}>
      {children}
      <button
        type="button"
        onClick={reveal}
        className="privacy-glass"
        data-revealed={revealed}
        // Hidden from assistive tech and skipped by the keyboard once revealed —
        // at that point it is decoration that hasn't been unmounted, purely so
        // the blur has something to animate back from.
        aria-hidden={revealed}
        tabIndex={revealed ? -1 : 0}
        aria-label={`Show ${label}`}
      >
        <span className="privacy-glass-hint" aria-hidden="true">
          <EyeIcon /> Show {label}
        </span>
      </button>
    </div>
  );
}

function EyeIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

/** The eye toggle for the top bar — flips every pane on the page together. */
export function PrivacyToggle({ className = '' }: { className?: string }) {
  const { revealed, toggle } = usePrivacy();
  return (
    <button
      onClick={toggle}
      className={`font-medium transition-opacity hover:opacity-100 ${className}`}
      style={{ opacity: 0.75 }}
      // The control's job is the *action*, so the name states what clicking does.
      aria-label={revealed ? 'Hide totals' : 'Show totals'}
      aria-pressed={!revealed}
      title={revealed ? 'Hide totals' : 'Show totals'}
    >
      {revealed ? <EyeIcon /> : <EyeOffIcon />}
    </button>
  );
}

function EyeOffIcon() {
  return (
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M10.6 5.1A10.9 10.9 0 0 1 12 5c6.5 0 10 7 10 7a18.3 18.3 0 0 1-2.4 3.4M6.2 6.2A18.1 18.1 0 0 0 2 12s3.5 7 10 7a10.7 10.7 0 0 0 5.8-1.7" />
      <path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      <path d="m2 2 20 20" />
    </svg>
  );
}
