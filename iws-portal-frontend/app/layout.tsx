import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

export const dynamic = 'force-dynamic';
import "./globals.css";
import StickyChrome from "./components/StickyChrome";
import IdleTimeout from "./IdleTimeout";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Rajani MIS Portal",
  description: "Internal MIS Dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <a
          href="#main-content"
          className="sr-only focus:not-sr-only focus:fixed focus:top-4 focus:left-4 focus:z-50 focus:px-4 focus:py-2 focus:bg-card focus:text-ink focus:rounded-md focus:shadow-lg focus:outline-none focus:ring-2 focus:ring-prime"
        >
          Skip to main content
        </a>
        <StickyChrome />
        <IdleTimeout />
        {children}
      </body>
    </html>
  );
}
