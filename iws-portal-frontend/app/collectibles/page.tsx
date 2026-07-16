'use client';
import GalleryAssetGrid from '@/app/components/GalleryAssetGrid';

export default function CollectiblesPage() {
  return (
    <GalleryAssetGrid
      category="collectibles"
      title="Collectibles"
      subtitle="Photos, location, provenance and current valuation"
      showPainter={false}
      emptyText="No collectibles recorded yet."
    />
  );
}
