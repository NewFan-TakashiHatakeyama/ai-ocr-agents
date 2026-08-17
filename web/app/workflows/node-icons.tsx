// SCR-07 ノードの種別アイコンとカテゴリ定義。エディタとパレットで共用する。
// 依存を増やさないため lucide 風の stroke SVG を最小構成でインライン定義。

export const CATEGORY: Record<string, { label: string; color: string }> = {
  source: { label: "トリガー", color: "var(--ok, #2e9e6b)" },
  process: { label: "処理", color: "var(--accent, #3b6ef5)" },
  branch: { label: "分岐", color: "var(--warn, #d98a1f)" },
  transform: { label: "変換", color: "#8a5cd6" },
  sink: { label: "出力", color: "#d65c7a" },
};

export const TYPE_LABEL: Record<string, string> = {
  "source.s3_event": "S3 イベント",
  "source.gdrive_event": "Google Drive",
  "source.manual": "手動実行",
  "source.schedule": "スケジュール",
  "source.email_attachment": "メール添付",
  "process.classify": "帳票分類",
  "process.extract": "AI-OCR 抽出",
  "branch.condition": "条件分岐",
  "branch.hitl_gate": "HITL ゲート",
  "transform.map_fields": "フィールド変換",
  "sink.db_write": "DB 格納",
  "sink.webhook": "Webhook",
  "sink.file": "ファイル出力",
  "sink.notify": "通知",
};

// 種別 → 24x24 stroke パス。d を複数持つものは配列で。
const ICON_PATHS: Record<string, { d?: string[]; circle?: [number, number, number][] }> = {
  "source.s3_event": {
    // クラウド + 取り込み矢印（S3 への着信でトリガー）
    d: ["M17.5 18a4.5 4.5 0 0 0 .4-8.98 6 6 0 0 0-11.7 1.2A4 4 0 0 0 7 18", "M12 10v8", "M9 15.5 12 18.5l3-3"],
  },
  "source.gdrive_event": {
    // Drive 風トライアングル + 取り込み矢印（フォルダ新着でトリガー）
    d: ["M9 4.5h6l5.5 9.5-3 5H6.5l-3-5z", "M12 9v6", "M9.5 12.5 12 15l2.5-2.5"],
  },
  "source.manual": { d: ["M8.5 5.5v13l10-6.5z"] },
  "source.schedule": { d: ["M12 7.5v4.5l3 2"], circle: [[12, 12, 8.5]] },
  "source.email_attachment": {
    d: ["M3.5 6.5h17v11h-17z", "M3.5 8l8.5 5.5L20.5 8"],
  },
  "process.classify": {
    d: ["M20.3 13.6 13.6 20.3a1.7 1.7 0 0 1-2.4 0L3.5 12.6V3.5h9.1l7.7 7.7a1.7 1.7 0 0 1 0 2.4z"],
    circle: [[7.8, 7.8, 1.4]],
  },
  "process.extract": {
    d: ["M13.5 3.5h-7a1.5 1.5 0 0 0-1.5 1.5v14a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5V9z", "M13.5 3.5V9H19", "M8.5 13.5h7", "M8.5 17h4.5"],
  },
  "branch.condition": { d: ["M12 3.5 20.5 12 12 20.5 3.5 12z"] },
  "branch.hitl_gate": {
    d: ["M4.5 19.5c0-2.8 2.2-4.7 4.7-4.7s4.7 1.9 4.7 4.7", "M15 11l2 2 3.7-3.7"],
    circle: [[9.2, 8.2, 3.3]],
  },
  "transform.map_fields": {
    d: ["M4 7.5h13", "M14 4.5l3 3-3 3", "M20 16.5H7", "M10 13.5l-3 3 3 3"],
  },
  "sink.db_write": {
    d: [
      "M5 6.5c0 1.66 3.13 3 7 3s7-1.34 7-3-3.13-3-7-3-7 1.34-7 3z",
      "M5 6.5v11c0 1.66 3.13 3 7 3s7-1.34 7-3v-11",
      "M5 12c0 1.66 3.13 3 7 3s7-1.34 7-3",
    ],
  },
  "sink.webhook": {
    d: [
      "M10 13.5a4.2 4.2 0 0 0 6 0l2.8-2.8a4.24 4.24 0 0 0-6-6L11.4 6.1",
      "M14 10.5a4.2 4.2 0 0 0-6 0l-2.8 2.8a4.24 4.24 0 0 0 6 6l1.4-1.4",
    ],
  },
  "sink.file": {
    d: ["M13.5 3.5h-7a1.5 1.5 0 0 0-1.5 1.5v14a1.5 1.5 0 0 0 1.5 1.5h11a1.5 1.5 0 0 0 1.5-1.5V9z", "M13.5 3.5V9H19", "M12 11.5v5", "M9.5 14l2.5 2.5 2.5-2.5"],
  },
  "sink.notify": {
    d: ["M18 9.5a6 6 0 0 0-12 0c0 5.5-2 6.5-2 6.5h16s-2-1-2-6.5", "M10.3 19.5a2 2 0 0 0 3.4 0"],
  },
};

const DEFAULT_ICON = { d: ["M4.5 4.5h15v15h-15z"] };

export function NodeIcon({ type, size = 16 }: { type: string; size?: number }) {
  const icon = ICON_PATHS[type] ?? DEFAULT_ICON;
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {(icon.d ?? []).map((d, i) => (
        <path key={i} d={d} />
      ))}
      {(icon.circle ?? []).map(([cx, cy, r], i) => (
        <circle key={`c${i}`} cx={cx} cy={cy} r={r} />
      ))}
    </svg>
  );
}
