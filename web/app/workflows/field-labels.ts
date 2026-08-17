// SCR-07 設定フォームの日本語化レイヤー。
//
// 設定フォームは gateway の /workflows/catalog が返す JSON Schema を rjsf が
// そのまま描画するため、ラベルが英語（pydantic のフィールド名を title-case した
// "Connection Id" 等）・スキーマ見出しがクラス名（"DbWriteConfig" 等）になる。
// バックエンドを変えず、描画直前にスキーマへ日本語の title/description を注入し、
// enum を「日本語ラベル付きの選択肢」に変換する。
//
// キーは **pydantic クラス名 + プロパティ名**。プロパティ名だけだと
// FieldMapping.format（書式変換）と FileSinkConfig.format（ファイル形式）のように
// 同名で意味が違う項目を取り違えるため。

import type { RJSFSchema, TranslatableString } from "@rjsf/utils";
import { englishStringTranslator } from "@rjsf/utils";

type FieldMeta = {
  label: string;
  help?: string;
  /** enum 値 → 日本語ラベル。指定すると選択肢が日本語で表示される */
  enums?: Record<string, string>;
};

type SchemaLabels = {
  /** スキーマ見出し（クラス名）の置き換え。未指定なら見出しを消す */
  __title?: string;
  [prop: string]: FieldMeta | string | undefined;
};

// pydantic クラス名 → { プロパティ名 → ラベル/ヘルプ/選択肢 }
const SCHEMA_LABELS: Record<string, SchemaLabels> = {
  // ---------- トリガー ----------
  S3EventConfig: {
    connection_id: { label: "接続先（S3）", help: "監視するバケットの接続を選びます" },
    prefix: {
      label: "対象フォルダ",
      help: "このフォルダ配下に置かれたファイルだけを取り込みます",
    },
    extensions: {
      label: "対象の拡張子",
      help: "取り込むファイルの拡張子（.pdf など。ドット始まり）",
    },
  },
  GDriveEventConfig: {
    connection_id: {
      label: "接続先（Google Drive）",
      help: "監視するドライブフォルダの接続を選びます（フォルダは接続側で設定）",
    },
    extensions: {
      label: "対象の拡張子",
      help: "取り込むファイルの拡張子（.pdf など。ドット始まり）",
    },
  },
  EmailAttachmentConfig: {
    connection_id: { label: "接続先（メール）" },
    from_filter: { label: "差出人で絞り込み", help: "任意。指定した差出人のメールだけ対象にします" },
    subject_filter: { label: "件名で絞り込み", help: "任意。件名にこの文字を含むメールだけ対象にします" },
  },
  ScheduleConfig: {
    cron: {
      label: "実行スケジュール",
      help: "「分 時 日 月 曜日」の5項目で指定。例: 0 9 * * 1（毎週月曜9時）",
    },
  },

  // ---------- 処理 ----------
  ClassifyConfig: {
    doc_types: {
      label: "対象の帳票種別",
      help: "処理を続ける帳票の種類（例: invoice, receipt）",
    },
    on_unknown: {
      label: "対象外だったとき",
      enums: { default_route: "そのまま次へ進む", halt: "ここで停止する" },
    },
  },
  ExtractConfig: {
    schema_id: {
      label: "使用するスキーマ",
      help: "抽出する項目を定義したスキーマの ID（スキーマ管理で作成）",
    },
    options: { label: "詳細設定" },
  },
  ExtractOptions: {
    __title: "詳細設定",
    force_vl: {
      label: "画像モデルを必ず使う",
      help: "文字認識が弱い帳票向け。通常はオフのままで構いません",
    },
  },

  // ---------- 分岐 ----------
  ConditionConfig: {
    branches: { label: "分岐条件" },
    else: {
      label: "どれにも当てはまらないとき",
      help: "全ての条件に一致しなかった帳票の送り先ノード ID",
    },
  },
  BranchArm: {
    __title: "条件",
    when: { label: "条件式", help: "例: fields.total_amount > 100000" },
    to: { label: "送り先ノード", help: "条件に一致したときに進むノードの ID" },
  },
  HitlGateConfig: {
    priority_boost: {
      label: "優先度の引き上げ",
      help: "レビュー待ち一覧で先に表示される度合い（0〜100）",
    },
    assignee_group: { label: "担当グループ", help: "任意。確認を担当するグループ名" },
    sla_hours: { label: "対応期限（時間）", help: "任意。この時間内の確認を目安にします" },
  },

  // ---------- 変換 ----------
  MapFieldsConfig: {
    mappings: { label: "項目の対応づけ" },
  },
  FieldMapping: {
    __title: "対応づけ",
    from: {
      label: "元の項目（抽出値）",
      help: "OCR で抽出した項目名。固定値を入れる場合は空欄にします",
    },
    to: { label: "出力先の項目名", help: "書き込み先の列名・キー名" },
    format: { label: "書式変換", help: "任意。値の整形ルール" },
    const: { label: "固定値", help: "抽出値ではなく決まった文字列を入れる場合に指定" },
    mask: { label: "値を伏せる（マスク）", help: "プレビューやログで値を隠します" },
  },

  // ---------- 出力 ----------
  DbWriteConfig: {
    connection_id: { label: "接続先データベース" },
    table: {
      label: "書き込み先テーブル",
      help: "「スキーマ.テーブル名」の形式。許可されたテーブルのみ書き込めます",
    },
    mode: {
      label: "書き込み方法",
      enums: { insert: "新規追加（INSERT）", upsert: "追加または更新（UPSERT）" },
    },
    keys: {
      label: "重複判定のキー列",
      help: "「追加または更新」のとき、同じ行とみなす列名（複数可）",
    },
    on_failure: {
      label: "失敗したときの動作",
      enums: {
        retry: "再試行する",
        skip_and_notify: "スキップして通知",
        halt_notify: "停止して通知",
      },
    },
  },
  WebhookSinkConfig: {
    connection_id: { label: "接続先（Webhook）" },
    payload_template: { label: "送信内容のテンプレート", help: "任意。省略時は既定の内容を送信します" },
  },
  FileSinkConfig: {
    connection_id: { label: "接続先（保存先）" },
    path: {
      label: "保存パス",
      help: "例: exports/{workflow_run_id}/{node_id}.json（{ } はプレースホルダ）",
    },
    format: { label: "ファイル形式", enums: { json: "JSON", csv: "CSV" } },
  },
  NotifyConfig: {
    connection_id: { label: "接続先（通知先）" },
    template: {
      label: "通知メッセージ",
      help: "例: 帳票 {document_id} を処理しました（{ } はプレースホルダ）",
    },
    when: { label: "送信条件", help: "任意。この条件が真のときだけ通知します（空なら常に送信）" },
  },
};

type SchemaNode = RJSFSchema & {
  $defs?: Record<string, RJSFSchema>;
  properties?: Record<string, RJSFSchema>;
  enum?: string[];
  anyOf?: SchemaNode[];
};

// Optional[X]（pydantic）は anyOf:[{type:X}, {type:null}] になり、rjsf が
// 「option 1 / option 2」という分かりにくい分岐セレクタを出す。null 分岐を畳んで
// 素の型にし、通常の入力欄1つにする（空欄は保存時に間引く→バックエンドで None 扱い）。
function collapseNullable(spec: SchemaNode): void {
  if (!Array.isArray(spec.anyOf)) return;
  const branches = spec.anyOf as SchemaNode[];
  const nonNull = branches.filter((s) => s?.type !== "null");
  if (spec.anyOf.length !== 2 || nonNull.length !== 1) return; // 単純な Optional のみ対象
  const branch = nonNull[0];
  delete spec.anyOf;
  // 型・制約を親へ引き上げる（title/description は既に日本語化済みなので保持）
  for (const [k, v] of Object.entries(branch)) {
    if (k === "title" || k === "description") continue;
    (spec as Record<string, unknown>)[k] = v;
  }
}

// 1 つのスキーマオブジェクト（config 本体 or $defs のエントリ）を日本語化する。
function localizeObject(obj: SchemaNode): void {
  const labels = typeof obj.title === "string" ? SCHEMA_LABELS[obj.title] : undefined;

  // 見出し（クラス名）は消すか日本語へ。パネル上部で既にノード名を出しているため
  // config 本体の見出しは基本的に不要。
  if (labels?.__title) obj.title = labels.__title;
  else delete obj.title;
  // pydantic のクラス docstring 由来の説明（技術的で長い）は隠す
  delete obj.description;

  if (!obj.properties) return;
  for (const [prop, rawSpec] of Object.entries(obj.properties)) {
    const spec = rawSpec as SchemaNode;
    const meta = labels?.[prop];
    if (!meta || typeof meta === "string") continue;
    spec.title = meta.label;
    if (meta.help) spec.description = meta.help;
    collapseNullable(spec);
    // enum → oneOf(const+title)。rjsf は const 列挙をラベル付き選択肢として描く
    if (meta.enums && Array.isArray(spec.enum)) {
      spec.oneOf = (spec.enum as string[]).map((v) => ({
        const: v,
        title: meta.enums?.[v] ?? v,
      }));
      delete spec.enum;
    }
  }
}

// ノード種別 → connection_id に許される接続 type。ID の手転記を避け、
// 該当 type の接続だけを選ばせる（DD-12: db_write は postgres 限定 等）。
const CONNECTION_TYPE_BY_NODE: Record<string, string> = {
  "source.s3_event": "s3",
  "source.gdrive_event": "gdrive",
  "sink.db_write": "postgres",
  "sink.webhook": "webhook",
  "sink.notify": "webhook",
  "sink.file": "s3",
};

type PickerSchema = { id: string; doc_type: string; version: number };
type PickerConn = { id: string; name: string; type: string };

/** schema_id / connection_id の文字列入力を、実在する ID の選択肢（oneOf）へ差し替える。
 *
 * gateway が返すスキーマ一覧・接続一覧から候補を作り、ユーザーが ID を手打ちせず
 * 選べるようにする（④-b の「ID 手転記」摩擦の解消）。現在値が候補に無い場合も
 * （消えた接続を参照している等）option として残し、値が黙って消えないようにする。 */
export function injectPickers(
  schema: RJSFSchema,
  opts: { nodeType: string; config: Record<string, unknown>; schemas: PickerSchema[]; connections: PickerConn[] },
): RJSFSchema {
  const clone = JSON.parse(JSON.stringify(schema)) as SchemaNode;
  const props = clone.properties;
  if (!props) return clone as RJSFSchema;

  const setOptions = (
    prop: string,
    options: { const: string; title: string }[],
    current: unknown,
  ) => {
    const spec = props[prop] as SchemaNode | undefined;
    if (!spec) return;
    const opts2 = [...options];
    if (typeof current === "string" && current && !opts2.some((o) => o.const === current)) {
      opts2.unshift({ const: current, title: `${current}（現在の値）` });
    }
    if (opts2.length === 0) return; // 候補ゼロなら素のテキスト入力のままにする
    spec.oneOf = opts2;
    delete spec.enum;
  };

  if ("schema_id" in props) {
    setOptions(
      "schema_id",
      opts.schemas.map((s) => ({ const: s.id, title: `${s.doc_type}（${s.id}・v${s.version}）` })),
      opts.config.schema_id,
    );
  }
  if ("connection_id" in props) {
    const want = CONNECTION_TYPE_BY_NODE[opts.nodeType];
    const conns = want ? opts.connections.filter((c) => c.type === want) : opts.connections;
    setOptions(
      "connection_id",
      conns.map((c) => ({ const: c.id, title: `${c.name || c.id}（${c.type}）` })),
      opts.config.connection_id,
    );
  }
  return clone as RJSFSchema;
}

/** 失敗トースト用: pydantic クラス名+プロパティ名 → 日本語ラベル。
 *
 * クラス名が分かればそれで引く。分からない/ネストのときは全スキーマ横断で
 * 一意に決まる場合のみ採用する（format/to/from のように同名で意味が違うものは
 * 訳さず英語のまま返す方が誤誘導しない）。 */
export function fieldLabel(schemaTitle: string | undefined, prop: string): string | undefined {
  if (schemaTitle) {
    const meta = SCHEMA_LABELS[schemaTitle]?.[prop];
    if (meta && typeof meta === "object") return meta.label;
  }
  let hit: string | undefined;
  for (const labels of Object.values(SCHEMA_LABELS)) {
    const meta = labels[prop];
    if (meta && typeof meta === "object") {
      if (hit && hit !== meta.label) return undefined; // 同名で意味が違う → 訳さない
      hit = meta.label;
    }
  }
  return hit;
}

/** catalog の JSON Schema を日本語ラベル付きへ変換（元は壊さずクローンを返す）。 */
export function localizeSchema(schema: RJSFSchema): RJSFSchema {
  const clone = JSON.parse(JSON.stringify(schema)) as SchemaNode;
  if (clone.$defs) {
    for (const def of Object.values(clone.$defs)) localizeObject(def as SchemaNode);
  }
  localizeObject(clone);
  return clone as RJSFSchema;
}

// rjsf 組み込みの英語文言（配列の Add/Remove など）を日本語へ。
const RJSF_STRINGS: Record<string, string> = {
  Add: "追加",
  "Add Item": "項目を追加",
  Remove: "削除",
  "Move up": "上へ",
  "Move down": "下へ",
  Copy: "複製",
  "No items yet. Use the button below to add some.": "まだ項目がありません。下のボタンで追加してください。",
  Yes: "はい",
  No: "いいえ",
};

/** config から空欄（""・null・undefined）を再帰的に間引く。
 *
 * Optional 項目は anyOf を畳んだ素の入力欄になるため、空にすると rjsf が "" を残す
 * ことがある。"" のまま送るとバックエンドで「値あり」と解釈され、例えば
 * FieldMapping の from/const 排他制約（どちらか一方）に誤って引っかかる。
 * 保存・点検・出力確認へ渡す前に間引き、未入力は「無し（None）」として送る。
 * 0 / false は意味を持つため残す。 */
export function pruneEmpty<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((v) => pruneEmpty(v)) as unknown as T;
  }
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (v === "" || v === null || v === undefined) continue;
      out[k] = pruneEmpty(v);
    }
    return out as T;
  }
  return value;
}

/** rjsf Form の translateString に渡す。既知の文言だけ日本語化し、残りは既定へ委譲。 */
export function jaTranslateString(stringToTranslate: string, params?: string[]): string {
  const jp = RJSF_STRINGS[stringToTranslate];
  if (jp && !params?.length) return jp;
  return englishStringTranslator(stringToTranslate as TranslatableString, params);
}
