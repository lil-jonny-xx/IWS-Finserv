'use client';
import { useRef } from 'react';
import type { HTMLAttributes, MouseEvent, CSSProperties } from 'react';

/**
 * Horizontal click-and-drag scrolling for wide tables.
 *
 * On a desktop without a horizontal-scroll trackpad, wide tables are awkward to
 * navigate. Press-and-drag left/right moves the table. Native wheel / vertical
 * scroll is unaffected; a small movement threshold preserves clicks on buttons,
 * links and sort headers (a real drag suppresses the click that follows).
 *
 * Two ways to use it:
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
    el.style.cursor = 'grabbing';
    el.style.userSelect = 'none';
  }

  function onMouseMove(e: MouseEvent<HTMLDivElement>) {
    const d = drag.current;
    const el = ref.current;
    if (!d.down || !el) return;
    const dx = e.pageX - d.startX;
    if (!d.moved && Math.abs(dx) < 5) return;   // threshold → preserve clicks
    d.moved = true;
    el.scrollLeft = d.startLeft - dx;
  }

  function end() {
    const el = ref.current;
    drag.current.down = false;
    if (el) { el.style.cursor = 'grab'; el.style.userSelect = ''; }
  }

  // If the press turned into a drag, swallow the click it would otherwise fire
  // (so dragging across a sort header / row link doesn't trigger it).
  function onClickCapture(e: MouseEvent<HTMLDivElement>) {
    if (drag.current.moved) {
      e.preventDefault();
      e.stopPropagation();
      drag.current.moved = false;
    }
  }

  const bind = {
    onMouseDown, onMouseMove, onMouseUp: end, onMouseLeave: end, onClickCapture,
    style: { cursor: 'grab' } as CSSProperties,
  };
  return { ref, bind };
}

export default function DragScroll({
  className = '', style, children, ...rest
}: HTMLAttributes<HTMLDivElement>) {
  const { ref, bind } = useDragScroll();
  return (
    <div
      ref={ref}
      className={className}
      {...bind}
      style={{ ...bind.style, ...style }}
      {...rest}
    >
      {children}
    </div>
  );
}
