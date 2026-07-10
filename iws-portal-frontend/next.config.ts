import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Production builds (www-data) use the default .next; set NEXT_DIST_DIR to
  // build as another user without touching the live output directory.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

export default nextConfig;
