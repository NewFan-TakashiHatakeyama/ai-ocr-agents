import { redirect } from "next/navigation";

// レビューキューは SCR-02 のタブに統合（画面設計書 §8.2）。後方互換のためリダイレクト。
export default function ReviewRedirect() {
  redirect("/documents?tab=queue");
}
