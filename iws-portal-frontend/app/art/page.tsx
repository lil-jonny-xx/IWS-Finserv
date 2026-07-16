'use client';
import GalleryAssetGrid from '@/app/components/GalleryAssetGrid';

export default function ArtPage() {
  return (
    <GalleryAssetGrid
      category="art"
      title="Art"
      subtitle="Paintings — painter, provenance, gallery and current valuation"
      showPainter
      emptyText="No paintings recorded yet."
    />
  );
}
