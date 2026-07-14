import { redirect } from "next/navigation";

// 入口は SCR-01 チャットホーム（画面設計書 §3）。
export default function Home() {
  redirect("/chat");
}
