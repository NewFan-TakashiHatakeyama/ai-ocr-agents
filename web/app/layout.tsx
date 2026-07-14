import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Toaster } from "@/components/Toaster";
import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "NewFan AI-OCR",
  description: "テンプレートレス AI-OCR — HITL 検証・レビューキュー（画面設計書 v1.0）",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="ja">
      <body>
        <Providers>
          {children}
          <Toaster />
        </Providers>
      </body>
    </html>
  );
}
