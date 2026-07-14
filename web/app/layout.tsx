import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "NewFan AI-OCR 検証UI",
  description: "HITL 検証・レビューキュー・ダッシュボード（§8）",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ja">
      <body>
        <Providers>
          <header className="header">
            <strong>NewFan AI-OCR</strong>
            <nav className="nav">
              <Link href="/documents">ドキュメント</Link>
              <Link href="/review">レビューキュー</Link>
            </nav>
          </header>
          {children}
        </Providers>
      </body>
    </html>
  );
}
