'use client';
// Full-screen photo viewer with zoom, shared by the Properties and Art /
// Collectibles registers — the pages where the photo IS the record (a painting's
// condition, a property's frontage) and a fit-to-screen thumbnail isn't enough.
//
// Interactions, all doing the same job so none has to be discovered:
//   wheel / pinch-trackpad → zoom about the cursor
//   double-click           → toggle between fit and 2×
//   drag                   → pan, only meaningful while zoomed in
//   +/− / 0 keys, buttons  → zoom in, out, reset
//   Esc or backdrop click  → close
//
// Panning is clamped so the image can never be dragged off-screen, and zooming
// back out re-centres it rather than leaving it stranded in a corner.
import { useCallback, useEffect, useRef, useState } from 'react';

const MIN_SCALE = 1;
const MAX_SCALE = 6;
const STEP = 1.4;

export default function PhotoLightbox({ src, alt, onClose }: {
  src: string; alt: string; onClose: () => void;
}) {
  const [scale, setScale] = useState(1);
  const [pos, setPos] = useState({ x: 0, y: 0 });
  const boxRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);
  // Mirrored in state because the render reads it (cursor + transition), and a
  // ref read during render neither re-renders nor is safe under concurrent React.
  const [dragging, setDragging] = useState(false);

  // At scale 1 the image fits, so there is nothing to pan; beyond that, allow
  // travel of half the overflow in each direction.
  const clamp = useCallback((x: number, y: number, s: number) => {
    const el = boxRef.current;
    if (!el || s <= 1) return { x: 0, y: 0 };
    const maxX = (el.clientWidth * (s - 1)) / 2;
    const maxY = (el.clientHeight * (s - 1)) / 2;
    return {
      x: Math.max(-maxX, Math.min(maxX, x)),
      y: Math.max(-maxY, Math.min(maxY, y)),
    };
  }, []);

  const zoomTo = useCallback((next: number) => {
    const s = Math.max(MIN_SCALE, Math.min(MAX_SCALE, next));
    setScale(s);
    setPos(p => clamp(p.x, p.y, s));
  }, [clamp]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { onClose(); return; }
      if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomTo(scale * STEP); }
      if (e.key === '-' || e.key === '_') { e.preventDefault(); zoomTo(scale / STEP); }
      if (e.key === '0')                  { e.preventDefault(); zoomTo(1); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, scale, zoomTo]);

  // Non-passive so the page behind cannot scroll while zooming the photo.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomTo(scale * (e.deltaY < 0 ? STEP : 1 / STEP));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [scale, zoomTo]);

  const onPointerDown = (e: React.PointerEvent) => {
    if (scale <= 1) return;
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { x: e.clientX, y: e.clientY, ox: pos.x, oy: pos.y };
    setDragging(true);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    setPos(clamp(d.ox + (e.clientX - d.x), d.oy + (e.clientY - d.y), scale));
  };
  const endDrag = () => { drag.current = null; setDragging(false); };

  return (
    <div
      className="fixed inset-0 z-[70] bg-ink/90 flex flex-col"
      role="dialog"
      aria-modal="true"
      aria-label={alt}
      onClick={onClose}
    >
      <div className="flex items-center justify-end gap-1 p-3 shrink-0" onClick={e => e.stopPropagation()}>
        <span className="mr-auto pl-2 text-xs text-card/70 truncate">{alt}</span>
        <LightboxButton label="Zoom out" onClick={() => zoomTo(scale / STEP)} disabled={scale <= MIN_SCALE}>−</LightboxButton>
        <span className="px-2 text-xs tabular-nums text-card/70 w-14 text-center">{Math.round(scale * 100)}%</span>
        <LightboxButton label="Zoom in" onClick={() => zoomTo(scale * STEP)} disabled={scale >= MAX_SCALE}>+</LightboxButton>
        <LightboxButton label="Reset zoom" onClick={() => zoomTo(1)} disabled={scale === 1}>Reset</LightboxButton>
        <LightboxButton label="Close" onClick={onClose}>✕</LightboxButton>
      </div>

      <div
        ref={boxRef}
        className="flex-1 overflow-hidden flex items-center justify-center select-none"
        onClick={e => e.stopPropagation()}
        onDoubleClick={() => zoomTo(scale > 1 ? 1 : 2)}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        style={{ cursor: scale > 1 ? (dragging ? 'grabbing' : 'grab') : 'zoom-in' }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={src}
          alt={alt}
          draggable={false}
          className="max-w-full max-h-full object-contain"
          style={{
            transform: `translate(${pos.x}px, ${pos.y}px) scale(${scale})`,
            // Only animate discrete zoom steps — animating the drag would lag it.
            transition: dragging ? 'none' : 'transform 140ms ease-out',
          }}
        />
      </div>
    </div>
  );
}

function LightboxButton({ children, label, onClick, disabled }: {
  children: React.ReactNode; label: string; onClick: () => void; disabled?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="px-2.5 py-1 rounded text-xs font-medium text-card/80 border border-card/25 hover:text-card hover:border-card/50 disabled:opacity-30 disabled:hover:text-card/80 disabled:hover:border-card/25 transition-colors"
    >
      {children}
    </button>
  );
}
