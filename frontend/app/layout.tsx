import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Valuation Agent - Frontend HTML",
  description: "Minimal web UI for the local valuation backend.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
