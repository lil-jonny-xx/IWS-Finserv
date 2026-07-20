'use client';
// The fixed chrome — ticker + session controls on top, section nav below —
// stuck to the viewport as one block.
//
// Sticking them as a unit rather than individually is deliberate: TopBar's height
// depends on the benchmark ticker, which renders nothing when it has no data, so
// a separately-stuck nav would need to know that height to offset itself and
// would overlap whenever the ticker appeared or vanished.
//
// The measured height is published as --chrome-h so content that sticks *below*
// the chrome (the dashboard's market rail) can offset itself without hardcoding a
// number that goes stale the moment either bar changes. globals.css carries a
// sensible default for first paint and for the server render.
import { useEffect, useRef } from 'react';
import TopBar from '../TopBar';
import GlobalNav from './GlobalNav';

export default function StickyChrome() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const publish = () =>
      document.documentElement.style.setProperty('--chrome-h', `${el.offsetHeight}px`);
    publish();
    const ro = new ResizeObserver(publish);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <div ref={ref} className="sticky top-0 z-50">
      <TopBar />
      <GlobalNav />
    </div>
  );
}
