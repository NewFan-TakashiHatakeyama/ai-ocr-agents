// 取り込める帳票の形式（唯一の定義）。
//
// 受理判定の正は ingest（services/ingest/src/newfan_ingest/validation.py の
// _EXT_TO_KIND とマジックバイト）で、ここはそれを画面に写したもの。Office
// （docx/xlsx/pptx）は validation が種別としては認めるが service が E1003
// 「Office→PDF 変換は未実装」で必ず拒否するので、**対応形式には含めない**。
// 以前はチャットの失敗トーストが「Word/Excel に対応」と書いていて、実際には
// 全件拒否されるのに利用者へ「対応している」と伝える嘘になっていた。
//
// ファイル選択の accept と、利用者向けの文言の両方をここから作る。二か所に
// 別々のリテラルを置くと、片方だけ直して食い違う（今回の原因そのもの）。

export interface AcceptedUploadType {
  /** ingest が原本に付ける MIME（accept 属性にも並べる） */
  mime: string;
  /** ドット始まり・小文字。ingest の拡張子照合と同じ綴り */
  extensions: readonly string[];
  /** 利用者向けの形式名 */
  label: string;
}

export const ACCEPTED_UPLOAD_TYPES: readonly AcceptedUploadType[] = [
  { mime: "application/pdf", extensions: [".pdf"], label: "PDF" },
  { mime: "image/png", extensions: [".png"], label: "PNG" },
  { mime: "image/jpeg", extensions: [".jpg", ".jpeg"], label: "JPEG" },
  { mime: "image/tiff", extensions: [".tif", ".tiff"], label: "TIFF" },
];

/** 受け付ける拡張子（ドット始まり・小文字）の一覧 */
export const ACCEPTED_UPLOAD_EXTENSIONS: readonly string[] = ACCEPTED_UPLOAD_TYPES.flatMap(
  (t) => t.extensions,
);

/**
 * `<input type="file" accept>` の値。MIME と拡張子の両方を並べる（ブラウザや OS に
 * よって MIME を持たないファイルがあり、片方だけだと選べない）。
 */
export const UPLOAD_ACCEPT: string = [
  ...ACCEPTED_UPLOAD_TYPES.map((t) => t.mime),
  ...ACCEPTED_UPLOAD_EXTENSIONS,
].join(",");

/** 利用者向けの対応形式の並び（「PDF / PNG / JPEG / TIFF」） */
export const UPLOAD_FORMATS_LABEL: string = ACCEPTED_UPLOAD_TYPES.map((t) => t.label).join(" / ");

/** 対応形式の説明文。取込に失敗したときの案内に使う */
export const UPLOAD_FORMATS_HINT = `対応形式は ${UPLOAD_FORMATS_LABEL} です。`;

// 単体の受理判定は ingest に任せ、拒否されたら UPLOAD_FORMATS_HINT を添える（ingest は
// 拡張子の無いファイルでもマジックバイトで受理するので、ここで弾くと正より厳しくなる）。
// 複数ファイルの一括投入だけは、件数を先に伝えるために lib/bulk.ts の partitionFiles が
// 投げる前に MIME / 拡張子で選別する。その一覧もここから作る（別のリテラルを置かない）。
