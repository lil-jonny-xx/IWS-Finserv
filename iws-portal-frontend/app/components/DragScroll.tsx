'use client';
import { useRef } from 'react';
import type { HTMLAttributes, MouseEvent } from 'react';

/**
 * Horizontal scrolling for wide tables.
 *
 * Two ways to scroll, neither of which fires on a plain hover:
 *   - touchpad / wheel horizontal scroll (native, via `overflow-x-auto`), and
 *   - press-and-drag left/right (hold the left button and move).
 *
 * Idle hover is left completely alone — no grab cursor, no captured events — so
 * tooltips and hover states work normally. The grabbing cursor and text-select
 * suppression only kick in once a real drag starts (>5px), and a real drag
 * suppresses the click that would otherwise follow (so dragging across a sort
 * header / row link doesn't trigger it).
 *
 *   <DragScroll className="overflow-x-auto">…</DragScroll>
 * or, to keep an existing wrapper div:
 *   const ds = useDragScroll();
 *   <div ref={ds.ref} {...ds.bind} className="overflow-auto …">…</div>
 */
export function useDragScroll() {
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef({ down: false, startX: 0, startLeft: 0, moved: false });

  function onMouseDown(e: MouseEvent<HTMLDivElement>) {
    if (e.button !== 0) return;                 // left button only
    const el = ref.current;
    if (!el) return;
    drag.current = { down: true, startX: e.pageX, startLeft: el.scrollLeft, moved: false };
  }

  function onMouseMove(e: MouseEvent<HTMLDivElement>) {
    const d = drag.current;
    const el = ref.current;
    if (!d.down || !el) return;
    const dx = e.pageX - d.startX;
    if (!d.moved && Math.abs(dx) < 5) return;   // threshold → preserve clicks & hover
    if (!d.moved) {                             // a real drag just started
      el.style.cursor = 'grabbing';
      el.style.userSelect = 'none';
    }
    d.moved = true;
    el.scrollLeft = d.startLeft - dx;
  }

  function end() {
    const el = ref.current;
    drag.current.down = false;
    if (el) { el.style.cursor = ''; el.style.userSelect = ''; }
  }

  // If the press turned into a drag, swallow the click it would otherwise fire.
  function onClickCapture(e: MouseEvent<HTMLDivElement>) {
    if (drag.current.moved) {
      e.preventDefault();
      e.stopPropagation();
      drag.current.moved = false;
    }
  }

  const bind = { onMouseDown, onMouseMove, onMouseUp: end, onMouseLeave: end, onClickCapture };
  return { ref, bind };
}

export default function DragScroll({
  className = '', style, children, ...rest
}: HTMLAttributes<HTMLDivElement>) {
  const { ref, bind } = useDragScroll();
  return (
    <div ref={ref} className={className} {...bind} style={style} {...rest}>
      {children}
    </div>
  );
}
