"use client";

// テンプレート化プレビュー（設計 §3）。全画面でページ画像を見ながら、
// 読み取りたい領域（include）と読み取りたくない領域（exclude）をドラッグで指定する。
//
// 最優先の制約は**テンプレートレスを壊さないこと**。矩形を一切触らずに保存した場合、
// 生成されるスキーマは領域機能を入れる前と完全に同一になる。発見済みフィールドの
// 位置は破線のゴーストとして「参考表示」するだけで、ユーザーがクリックして確定した
// ものと自分で描いたものだけが保存対象になる（オプトイン）。
//
// メッセージにトーストを使わない: .toast-wrap は z-index 50、このプレビューは 100 で、
// 表示中に出したトーストは幕の下に沈んで × も押せない（warn は自動消去もされない）。
// 検証・API エラーは保存ボタン近傍のインライン領域に出す。
//
// 幕（.tpl-overlay）は **document.body へポータルで描く**。検証画面のヘッダ（.rv-head）は
// backdrop-filter を持ち、backdrop-filter / transform / filter を持つ祖先は position: fixed の
// 包含ブロックになる。ヘッダの「✎ 領域・項目を編集」から開くと幕がヘッダ（高さ約 57px）に
// 閉じ込められ、中央寄せの本体（高さ約 870px）の上 400px ほどが画面外に出て、ページ切替・
// ズーム・読取／除外の切替に手が届かなかった（2026-09-24 に実画面で確認）。
// ページのショートカット抑止は document.querySelector(".tpl-overlay") で幕の有無を見るので、
// ポータルにしても効く。

import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { RegionCanvas, type CanvasGhost, type CanvasRegion, type Px } from "@/components/RegionCanvas";
import { ApiError, api } from "@/lib/api";
import { TYPE_OPTIONS, guessFieldType } from "@/lib/fieldTypes";
// 保存 body の生成・検査・ゴースト解決・例示値の計算は純粋関数（lib/templatize）に
// 置き、ここでは state とイベントだけを持つ。単体テストはそちらにだけ付ける。
import {
  buildSaveBody,
  denormalize,
  exampleValueFromQuote,
  exampleValueFromSpans,
  padOf,
  resolveGhosts,
  resolvePage,
  spansExcludedByRect,
  truncateChars,
  validateDrafts,
  type DraftRow,
  type Preserved,
  type PreviewRegion,
} from "@/lib/templatize";
// 記入例語の判定はサーバ（orchestrator の事前ガードと同じ関数）に問い合わせる。
// ここで規則を書き直さない（lib/placeholderExample の冒頭）
import {
  PLACEHOLDER_ROW_NOTE,
  isPlaceholderExample,
  mergeVerdicts,
  unknownExampleValues,
} from "@/lib/placeholderExample";
import type { SchemaSaved } from "@/lib/useSchemaSaved";
import { newUuid } from "@/lib/uuid";
import type { ExtractedField, PageDim, RegionRect, RunSpans } from "@/lib/types";

// 型の選択肢は lib/fieldTypes.ts（スキーマ管理画面と共有）。
// この画面で足した新規行の型の選択肢（設計 v2 D7）。table を除く —— 列定義の無い
// 表項目ができてしまう。既存行は従来どおり（読み取り専用の table 行を表示できる）。
const NEW_ROW_TYPE_OPTIONS = TYPE_OPTIONS.filter(([v]) => v !== "table");

/** この領域に保存される例示値（画面で取った値か、読み込んだ原本のもの）。無ければ null。 */
function exampleForRegion(r: PreviewRegion): string | null {
  if (r.exampleValue !== undefined) return r.exampleValue;
  return r.origin?.example_value ?? null;
}

type Selection = { kind: "row"; rowId: string } | { kind: "region"; regionId: string } | null;

function emptyRow(): DraftRow {
  return { rowId: newUuid(), name: "", label: "", type: "string", include: true, sample: "", isNew: true };
}

// キャンバスの表示倍率（fit の何倍か。設計 §3.4）。fit では A4@250dpi で最小矩形 8 表示 px
// ≈ 23 画像 px と粗く、日付・単価セルの指定がつらい（§11-4）。
const ZOOM_LEVELS = [1, 1.5, 2, 3] as const;
type ZoomLevel = (typeof ZOOM_LEVELS)[number];
// 除外行の「この領域内の文字」に添える原文の上限（読み順に連結して切る）
const EXCLUDE_PREVIEW_MAX_LEN = 40;

export function TemplatizePreview({
  documentId,
  fields,
  pages,
  mode,
  docType: initialDocType,
  onClose,
  onSaved,
}: {
  documentId: string;
  fields: ExtractedField[];
  pages: PageDim[];
  mode: "create" | "edit";
  docType?: string | null;
  onClose: () => void;
  // 保存後の案内（領域なしの新規項目・例示値・記入例語の警告）は useSchemaSaved が出す
  onSaved: (r: SchemaSaved) => void;
}) {
  const pageCount = Math.max(pages.length, 1);
  const dimsByPage = useMemo(() => {
    const m = new Map<number, PageDim>();
    pages.forEach((p) => m.set(p.page_no, p));
    return m;
  }, [pages]);

  const [docType, setDocType] = useState(initialDocType ?? "");
  const [drafts, setDrafts] = useState<DraftRow[]>([]);
  const [regions, setRegions] = useState<PreviewRegion[]>([]);
  const [selection, setSelection] = useState<Selection>(null);
  const [drawMode, setDrawMode] = useState<"include" | "exclude">("include");
  const [page, setPage] = useState(1);
  const [zoom, setZoom] = useState<ZoomLevel>(1);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(mode === "edit");
  const [prev, setPrev] = useState<{ id: string; version: number } | null>(null);
  // **編集できないまま保全する領域**。ページ寸法（pages.width/height）が取れないと
  // 正規化⇄画素の変換ができず矩形を描けないが、だからといって落としてはいけない。
  // 落とすと保存が全置換なので、開いて保存しただけで既存の設定が消える
  // （実機で再現した事故: ページ寸法 API が失敗した窓で領域が全消去された）。
  // 読み込んだ値をそのまま持ち回し、保存時に無変換で戻す。
  const [preserved, setPreserved] = useState<Preserved>({
    fieldRegions: {},
    excludes: [],
    sourcePageCount: null,
  });
  // 「＋ 領域から項目を追加」の一回限りモード（設計 v2 D1）。on のあいだ次のドラッグが
  // 新しい項目になる。暗黙に新規項目を作る経路は作らない —— 「行を選ばずに引くと新規」
  // は選択の有無が画面から見えず、誤操作で行が増える（敵対的レビュー R4）。
  const [addingFromRegion, setAddingFromRegion] = useState(false);
  // 足した直後の行の表示名にフォーカスを移す（autoFocus はマウント時にだけ効くので、
  // 新しい行にだけ付ける）
  const [focusRowId, setFocusRowId] = useState<string | null>(null);
  // GET /documents/{id}/spans の応答をページごとにキャッシュ（設計 v2 D11・D12）。
  // 手描きの枠の下の文字を例示値にし、「枠に含まれる文字」の表示にも使う。
  // null = 取得に失敗（表示は「見つかりません」、例示値は null）。失敗はキャッシュ
  // しない（次に枠を引いたときに取り直す）。取得は非同期・失敗許容で、画面を止めない。
  const [spansByPage, setSpansByPage] = useState<Map<number, RunSpans | null>>(() => new Map());
  const spansInflight = useRef<Map<number, Promise<RunSpans | null>>>(new Map());
  const loadSpans = useCallback(
    (p: number): Promise<RunSpans | null> => {
      const inflight = spansInflight.current.get(p);
      if (inflight) return inflight;
      const pr = api
        .getRunSpans(documentId, p)
        .then(
          (run) => run,
          () => null,
        )
        .then((run) => {
          if (run) {
            setSpansByPage((m) => new Map(m).set(p, run));
          } else {
            spansInflight.current.delete(p); // 失敗は残さない（次回に取り直す）
            setSpansByPage((m) => new Map(m).set(p, null));
          }
          return run;
        });
      spansInflight.current.set(p, pr);
      return pr;
    },
    [documentId],
  );

  // ページ寸法がまったく無い＝領域の編集自体が成立しない。保存は既存値の保全に
  // 徹し、利用者には理由を出す（黙って「何も無い」画面を見せない）。
  const dimsUnavailable = pages.length === 0;

  // --- 初期化 ---
  useEffect(() => {
    if (mode === "create") {
      setDrafts(
        fields.map((f) => {
          const sample = f.value_normalized ?? f.value_raw ?? "";
          return {
            rowId: newUuid(),
            name: f.name,
            label: f.label ?? f.name,
            type: guessFieldType(sample),
            include: Boolean(sample), // 値が取れなかった項目は既定で外す
            sample,
          };
        }),
      );
      setLoading(false);
      return;
    }
    // 編集モード: doc_type 起点で最新版をプリロードする。
    // list_schemas は doc_type ごと最新版しか返さず、run.schema_id は抽出時点の
    // 旧版であり得るため id 突合はできない。
    let active = true;
    if (!initialDocType) {
      setErr("編集対象のスキーマを特定できませんでした。");
      setLoading(false);
      return;
    }
    api
      .getSchema(initialDocType)
      .then((s) => {
        if (!active) return;
        setPrev({ id: s.id, version: s.version });
        const byName = new Map(fields.map((f) => [f.name, f]));
        const rows: DraftRow[] = s.fields.map((f) => ({
          rowId: newUuid(),
          name: f.name,
          label: f.label ?? f.name,
          type: f.type,
          include: true,
          sample: byName.get(f.name)?.value_normalized ?? byName.get(f.name)?.value_raw ?? "",
          // 未知キーを含めて丸ごと保全する。put_schema は常に全置換なので、
          // ここで落とすと required / critical / columns が新版で全滅する。
          base: f,
        }));

        const loaded: PreviewRegion[] = [];
        const keptFields: Record<string, RegionRect> = {};
        const keptExcludes: RegionRect[] = [];
        s.fields.forEach((f, i) => {
          if (!f.region) return;
          const resolved = resolvePage(f.region.page, pageCount);
          const d = dimsByPage.get(resolved);
          if (!d?.width || !d?.height) {
            // 寸法が無いページの領域は画素に戻せない → **編集させず、そのまま保全する**
            keptFields[rows[i].rowId] = f.region;
            return;
          }
          const px = denormalize(f.region.rect, d.width, d.height);
          loaded.push({
            id: newUuid(),
            kind: "include",
            bbox: px,
            drawnPage: resolved,
            page: resolved,
            rowId: rows[i].rowId,
            origin: f.region,
            originBbox: px,
          });
        });
        (s.exclude_regions ?? []).forEach((r) => {
          const resolved = resolvePage(r.page, pageCount);
          const d = dimsByPage.get(resolved);
          if (!d?.width || !d?.height) {
            keptExcludes.push(r);
            return;
          }
          const px = denormalize(r.rect, d.width, d.height);
          loaded.push({
            id: newUuid(),
            kind: "exclude",
            bbox: px,
            drawnPage: resolved,
            page: r.page ?? null,
            label: r.label ?? undefined,
            origin: r,
            originBbox: px,
          });
        });

        setDrafts(rows);
        setRegions(loaded);
        setPreserved({
          fieldRegions: keptFields,
          excludes: keptExcludes,
          // **テンプレート化した時点のページ数**は編集で書き換えない。編集画面を
          // 開いた帳票のページ数で上書きすると、触っていないのに位置ガードの
          // ページ判定条件が変わる（「矩形を触らず保存すれば完全一致」が破れる）。
          sourcePageCount: s.source_page_count ?? null,
        });
        setLoading(false);
      })
      .catch((e) => {
        if (!active) return;
        setErr(`既存のスキーマを読み込めませんでした（${(e as Error).message}）。`);
        setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, initialDocType]);

  // --- 派生 ---
  const regionByRow = useMemo(() => {
    const m = new Map<string, PreviewRegion>();
    regions.forEach((r) => r.kind === "include" && r.rowId && m.set(r.rowId, r));
    return m;
  }, [regions]);

  const activeRowId =
    selection?.kind === "row"
      ? selection.rowId
      : selection?.kind === "region"
        ? (regions.find((r) => r.id === selection.regionId)?.rowId ?? null)
        : null;
  const selectedRegionId =
    selection?.kind === "region"
      ? selection.regionId
      : selection?.kind === "row"
        ? (regionByRow.get(selection.rowId)?.id ?? null)
        : null;

  const ghosts: CanvasGhost[] = useMemo(
    () => resolveGhosts({ fields, drafts, regionByRow, page, mode }),
    [fields, drafts, page, regionByRow, mode],
  );

  // Esc で「領域から項目を追加」モードを抜ける。RegionCanvas の Escape ハンドラは
  // onSelect(null) を呼ぶだけ（同じ window で stopPropagation しても他のリスナは
  // 止まらない）なので、ここで別に拾う。
  useEffect(() => {
    if (!addingFromRegion) return;
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") setAddingFromRegion(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [addingFromRegion]);

  // 選択中の読取領域について「枠に含まれる文字」を出すために、そのページの span を
  // 引く（未取得のときだけ。失敗済み＝null も取り直さない —— 選択のたびに失敗する
  // 要求を繰り返さない。枠を引き直せば取り直す）。
  const selectedInclude =
    selectedRegionId !== null
      ? (regions.find((r) => r.id === selectedRegionId && r.kind === "include") ?? null)
      : null;
  const selectedIncludePage = selectedInclude?.drawnPage ?? null;
  useEffect(() => {
    if (selectedIncludePage === null) return;
    // 取得済み（RunSpans）なら何もしない。失敗（null）は行を選び直したときに取り直す
    if (spansByPage.get(selectedIncludePage)) return;
    void loadSpans(selectedIncludePage);
    // spansByPage は読むだけ（取得の完了で変わっても取り直す必要は無い）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIncludePage, loadSpans]);

  const canvasRegions: CanvasRegion[] = regions
    .filter((r) => r.drawnPage === page)
    .map((r) => ({
      id: r.id,
      bbox: r.bbox,
      kind: r.kind,
      label:
        r.kind === "include"
          ? (drafts.find((d) => d.rowId === r.rowId)?.label || "新しい項目")
          : (r.label || "除外"),
    }));

  const excludes = regions.filter((r) => r.kind === "exclude");
  const includes = regions.filter((r) => r.kind === "include");

  // 例示値が記入例語か（設計 region-field-add-and-hint-v2 §2.5）。例示値が決まった時点
  // （ゴーストのクリック・手描きの span 取得・既存版の読み込み）で、未判定のものだけを
  // まとめてサーバに問い合わせる。判定は保存される値（exampleForRegion）で行い、同じ値は
  // 2 度問い合わせない。**失敗しても画面は止めない**（注記が出ないだけ。失敗した値は
  // 例示値の組が変わったときに問い合わせ直す ── 同じ組で失敗を繰り返さない）。
  const [placeholderVerdicts, setPlaceholderVerdicts] = useState<Map<string, boolean>>(
    () => new Map(),
  );
  const placeholderInflight = useRef<Set<string>>(new Set());
  const uncheckedExamples = unknownExampleValues(
    includes.map(exampleForRegion),
    placeholderVerdicts,
  );
  const uncheckedExamplesKey = JSON.stringify(uncheckedExamples);
  useEffect(() => {
    const pending = uncheckedExamples.filter((v) => !placeholderInflight.current.has(v));
    if (pending.length === 0) return;
    pending.forEach((v) => placeholderInflight.current.add(v));
    api
      .checkExampleValues(pending)
      .then(
        (res) => setPlaceholderVerdicts((m) => mergeVerdicts(m, pending, res)),
        () => {
          // 補助情報。取れなくても領域の指定・保存はできる（保存後の通知はサーバの応答で出る）
        },
      )
      .finally(() => pending.forEach((v) => placeholderInflight.current.delete(v)));
    // 値の組（uncheckedExamplesKey）が変わったときだけ問い合わせる。配列そのものは
    // 描画ごとに作り直されるので依存に入れない
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uncheckedExamplesKey]);

  // 除外行の「この領域内の文字」（この帳票で実際に消える span）のために、除外の引かれた
  // ページの span を引く（未取得のときだけ。失敗＝null は除外を引き直したときに取り直す）。
  // 除外は「消しすぎ」が危険なので、保存する前に何が消えるかを見せる
  const excludePagesKey = Array.from(new Set(excludes.map((r) => r.drawnPage)))
    .sort((a, b) => a - b)
    .join(",");
  useEffect(() => {
    if (!excludePagesKey) return;
    for (const p of excludePagesKey.split(",").map(Number)) {
      if (spansByPage.get(p)) continue;
      void loadSpans(p);
    }
    // spansByPage は読むだけ（取得の完了で変わっても取り直す必要は無い）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [excludePagesKey, loadSpans]);

  // --- 操作 ---
  function patchRow(rowId: string, p: Partial<DraftRow>) {
    setDrafts((ds) => ds.map((d) => (d.rowId === rowId ? { ...d, ...p } : d)));
  }

  function setRowInclude(rowId: string, on: boolean) {
    patchRow(rowId, { include: on });
    if (!on) {
      // 領域を持つ行のチェックを外したら矩形も消す。残すと「見えているのに
      // 保存されない」状態になり、消えた理由が画面から分からない。
      setRegions((rs) => rs.filter((r) => !(r.kind === "include" && r.rowId === rowId)));
    }
  }

  /**
   * 行に読取領域を付ける（既にあれば置き換える）。出どころ（設計 v2 D8・D11）:
   *   - ghost: AI が見つけた位置をクリックで採った。例示値はその項目の source_quote
   *   - manual: 手描き。例示値は枠の下の span の原文で、取得は非同期（取れるまで
   *     undefined、取れなければ null）。**引き直したら必ず取り直す** —— 領域は id ごと
   *     置き換わるので、古い取得が後から解決しても（id が無いので）何も上書きしない。
   */
  function addInclude(
    rowId: string,
    bbox: Px,
    from: { originKind: "ghost"; exampleValue: string | null } | { originKind: "manual" },
  ) {
    const id = newUuid();
    const drawnPage = page;
    const created: PreviewRegion = {
      id,
      kind: "include",
      bbox,
      drawnPage,
      page: drawnPage,
      rowId,
      originKind: from.originKind,
      createdAt: new Date().toISOString(),
      exampleValue: from.originKind === "ghost" ? from.exampleValue : undefined,
    };
    setRegions((rs) => [
      ...rs.filter((r) => !(r.kind === "include" && r.rowId === rowId)), // 置換
      created,
    ]);
    patchRow(rowId, { include: true }); // 領域指定＝抽出したいの意思表示
    setErr(null);
    if (from.originKind === "manual") {
      void loadSpans(drawnPage).then((run) => {
        const value = exampleValueFromSpans(run, drawnPage, bbox);
        setRegions((rs) => rs.map((r) => (r.id === id ? { ...r, exampleValue: value } : r)));
      });
    }
  }

  // 「＋ 項目を追加」（D2）: 領域の無い空行を末尾に足す（あとで行を選んで引ける）
  function addBlankRow() {
    const d = emptyRow();
    setDrafts((ds) => [...ds, d]);
    setSelection({ kind: "row", rowId: d.rowId });
    setFocusRowId(d.rowId);
    setAddingFromRegion(false);
    setErr(null);
  }

  // 「＋ 領域から項目を追加」のドラッグ（D1）: 空行と、いま引いた矩形を同時に足して
  // モードを抜け、表示名の入力へ
  function addRowFromRegion(bbox: Px) {
    const d = emptyRow();
    setDrafts((ds) => [...ds, d]);
    addInclude(d.rowId, bbox, { originKind: "manual" });
    setAddingFromRegion(false);
    setSelection({ kind: "row", rowId: d.rowId });
    setFocusRowId(d.rowId);
  }

  // 新規行の「×」: 行と、その行に紐づく読取領域を一緒に消す（孤立領域を残さない）
  function removeNewRow(rowId: string) {
    setDrafts((ds) => ds.filter((d) => d.rowId !== rowId));
    setRegions((rs) => rs.filter((r) => !(r.kind === "include" && r.rowId === rowId)));
    setSelection((s) => {
      if (!s) return s;
      if (s.kind === "row" && s.rowId === rowId) return null;
      if (s.kind === "region" && regionByRow.get(rowId)?.id === s.regionId) return null;
      return s;
    });
    setErr(null);
  }

  function onDraw(bbox: Px) {
    if (addingFromRegion) {
      addRowFromRegion(bbox);
      return;
    }
    if (drawMode === "exclude") {
      const id = newUuid();
      setRegions((rs) => [
        ...rs,
        { id, kind: "exclude", bbox, drawnPage: page, page, label: undefined },
      ]);
      setSelection({ kind: "region", regionId: id });
      setErr(null);
      return;
    }
    if (!activeRowId) {
      setErr("読取領域を紐づける項目を、右の一覧から選んでください。");
      return;
    }
    addInclude(activeRowId, bbox, { originKind: "manual" });
  }

  function onGhostClick(name: string) {
    const d = drafts.find((x) => x.name === name);
    const f = fields.find((x) => x.name === name);
    if (!d || !f?.bbox) return;
    const dim = dimsByPage.get(page);
    const W = dim?.width ?? 0;
    const H = dim?.height ?? 0;
    const p = padOf(W || 1000, H || 1000);
    const [x1, y1, x2, y2] = f.bbox as Px;
    addInclude(
      d.rowId,
      [
        Math.max(0, x1 - p),
        Math.max(0, y1 - p),
        Math.min(W || x2 + p, x2 + p),
        Math.min(H || y2 + p, y2 + p),
      ],
      // 例示値は AI が根拠にした span の原文（無ければ null）。正規化後の値は使わない
      { originKind: "ghost", exampleValue: exampleValueFromQuote(f.source_quote) },
    );
    setSelection({ kind: "row", rowId: d.rowId });
  }

  function deleteRegion(id: string) {
    setRegions((rs) => rs.filter((r) => r.id !== id));
    setSelection(null);
  }

  // 背景を押したときは矩形の選択だけ外し、対象項目の選択は残す
  // （残さないと「項目を選ぶ → ドラッグで描く」が成立しない）
  function onBackgroundDown() {
    if (!selection || selection.kind === "row") return;
    const r = regions.find((x) => x.id === selection.regionId);
    setSelection(r?.rowId ? { kind: "row", rowId: r.rowId } : null);
  }

  // --- 保存 ---
  async function save() {
    const problem = validateDrafts({ docType, drafts, includes });
    if (problem) {
      setErr(problem);
      return;
    }
    // 手描き領域の例示値は非同期に取っている。取得が終わる前に保存すると example_value
    // が載らず、ヒント on 後にその項目だけ kind_conflict ガードが効かない（レビュー指摘）。
    // 未取得のものはここで待って埋める（失敗は null のまま送る＝サーバ既定と同じ）。
    setBusy(true);
    const pending = regions.filter(
      (r) => r.kind === "include" && r.originKind === "manual" && r.exampleValue === undefined,
    );
    let regionsForSave = regions;
    if (pending.length > 0) {
      const filled = await Promise.all(
        pending.map(async (r) => {
          const run = await loadSpans(r.drawnPage);
          return [r.id, exampleValueFromSpans(run, r.drawnPage, r.bbox)] as const;
        }),
      );
      const byId = new Map(filled);
      regionsForSave = regions.map((r) =>
        byId.has(r.id) ? { ...r, exampleValue: byId.get(r.id) ?? null } : r,
      );
      setRegions(regionsForSave);
    }
    const body = buildSaveBody({
      drafts,
      regions: regionsForSave,
      preserved,
      dimsUnavailable,
      pageCount,
      pageDims: pages,
      mode,
    });
    // 領域の無い新規項目は保存を止めない（D2）。代わりに保存後の案内で
    // 「AI が語彙から探す」ことを伝える
    const newWithoutRegion = drafts.filter(
      (d) => d.isNew && d.include && !regionByRow.has(d.rowId),
    ).length;
    const withExampleValue = body.fields.filter((f) => f.region?.example_value).length;

    setBusy(true);
    setErr(null);
    try {
      const saved = await api.putSchema(docType.trim(), body.fields, {
        create: mode === "create",
        excludeRegions: body.excludeRegions,
        sourcePageCount: body.sourcePageCount,
      });
      onSaved({
        docType: saved.doc_type,
        schemaId: saved.id,
        version: saved.version,
        prevSchemaId: prev?.id ?? null,
        newWithoutRegion,
        withExampleValue,
        // 記入例語の例示値はサーバが**保存した版**で判定し直したもの（画面の注記が
        // 取れていなくても、保存後の通知はこれで必ず出る）
        warnings: saved.warnings ?? [],
        fieldLabels: Object.fromEntries(body.fields.map((f) => [f.name, f.label || f.name])),
      });
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setErr("同名のスキーマが既にあります。別の名前にするか、スキーマ管理から編集してください。");
      } else if (e instanceof ApiError && e.status === 403) {
        setErr("スキーマの保存には管理者権限が必要です。");
      } else if (e instanceof ApiError && e.status === 422) {
        setErr(`指定した領域が保存できません（${e.message}）。矩形を描き直してください。`);
      } else {
        setErr(`保存できませんでした（${(e as Error).message}）。`);
      }
      setBusy(false);
    }
  }

  const activeRow = drafts.find((d) => d.rowId === activeRowId);
  const preservedCount =
    Object.keys(preserved.fieldRegions).length + preserved.excludes.length;

  const overlay = (
    <div className="tpl-overlay" role="dialog" aria-modal="true" aria-label="テンプレート化プレビュー">
      <div className="rgn-shell">
        <header className="rgn-head">
          <b>
            {mode === "create" ? "この抽出結果をテンプレート化" : "領域・項目を編集"}
            {mode === "edit" && prev && (
              <span className="sub">
                {" "}
                · スキーマ v{prev.version} → <b>v{prev.version + 1}</b> として保存します
              </span>
            )}
          </b>
          <span className="spacer" />
          {/* 「領域から項目を追加」中は読取領域に固定する（次のドラッグは新しい項目） */}
          <div className="rgn-modes" role="group" aria-label="描画モード">
            <button
              className={`btn sm${drawMode === "include" ? " primary" : ""}`}
              onClick={() => setDrawMode("include")}
              disabled={addingFromRegion}
            >
              読取領域
            </button>
            <button
              className={`btn sm${drawMode === "exclude" ? " primary" : ""}`}
              onClick={() => setDrawMode("exclude")}
              disabled={addingFromRegion}
            >
              除外領域
            </button>
          </div>
          <button className="btn sm ghost" onClick={onClose} aria-label="閉じる">
            ×
          </button>
        </header>

        <div className="rgn-body">
          <div className="rgn-left">
            <div className="v-tools">
              {Array.from({ length: pageCount }, (_, i) => i + 1).map((p) => (
                <button
                  key={p}
                  className={`pagetab${p === page ? " on" : ""}`}
                  onClick={() => {
                    setPage(p);
                    setSelection(null);
                  }}
                >
                  p.{p}
                </button>
              ))}
              <span className="rgn-zoom" role="group" aria-label="表示倍率">
                {ZOOM_LEVELS.map((z) => (
                  <button
                    key={z}
                    className={`pagetab${zoom === z ? " on" : ""}`}
                    onClick={() => setZoom(z)}
                    aria-pressed={zoom === z}
                    title={
                      z === 1
                        ? "ページ全体を表示"
                        : `全体表示の ${z} 倍。日付・単価セルなど小さな項目を正確に囲むとき（スクロールできます）`
                    }
                  >
                    {z === 1 ? "全体" : `${Math.round(z * 100)}%`}
                  </button>
                ))}
              </span>
              <span className="spacer" />
              <span className="sub">
                {addingFromRegion
                  ? "次のドラッグが新しい項目の読取領域になります"
                  : drawMode === "include"
                    ? "破線＝AI が見つけた位置。クリックで確定、または空きをドラッグ"
                    : "読ませたくない範囲（印影・ロゴ等）をドラッグ"}
              </span>
            </div>
            {addingFromRegion && (
              <div className="rgn-band" role="status">
                <b>新しい項目の位置をドラッグしてください</b>（Esc で中止）
                <span className="spacer" />
                <button className="btn sm" onClick={() => setAddingFromRegion(false)}>
                  中止
                </button>
              </div>
            )}
            <RegionCanvas
              documentId={documentId}
              pageNo={page}
              pageDim={dimsByPage.get(page)}
              regions={canvasRegions}
              // 追加モード中はゴーストを出さない: ゴーストの上ではドラッグが始まらず、
              // 「次のドラッグが新しい項目」という約束が崩れる
              ghosts={drawMode === "include" && !addingFromRegion ? ghosts : []}
              mode={drawMode}
              zoom={zoom}
              selectedId={selectedRegionId}
              onDraw={onDraw}
              onSelect={(id) => setSelection(id ? { kind: "region", regionId: id } : null)}
              onBackgroundDown={onBackgroundDown}
              onGhostClick={onGhostClick}
              onDelete={deleteRegion}
            />
          </div>

          <aside className="rgn-right">
            {loading ? (
              <p className="sub">読み込み中…</p>
            ) : (
              <>
                <label className="extract-field">
                  <span>帳票種別（doc_type）</span>
                  <input
                    value={docType}
                    onChange={(e) => setDocType(e.target.value)}
                    placeholder="例: invoice, quotation, 納品書"
                    disabled={mode === "edit"}
                  />
                </label>
                {mode === "edit" && (
                  <p className="sub" style={{ margin: 0 }}>
                    {/* 項目の追加はこの画面でできる（D3）。削除は map_fields・学習メモリの
                        キーに波及するので引き続きスキーマ管理画面で（D4）。行き止まりに
                        しないよう、その画面へのリンクを添える */}
                    項目の<b>削除</b>と必須／重要の設定は
                    <Link href="/schemas" target="_blank" rel="noopener">
                      スキーマ管理画面
                    </Link>
                    （別タブで開きます）で行います。ここでは
                    項目の追加と、領域の指定・表示名・型の修正ができます。
                  </p>
                )}

                <div className="rgn-rows">
                  <div className="rgn-row rgn-head-row">
                    <span>{mode === "create" ? "残す" : "領域"}</span>
                    <span>表示名 / 項目名</span>
                    <span>型</span>
                    <span>位置</span>
                  </div>
                  {drafts.map((d) => {
                    const r = regionByRow.get(d.rowId);
                    const on = activeRowId === d.rowId;
                    // 選択中の読取領域なら、**保存される例示値と同じもの**を出す。
                    // ゴースト由来は AI が読んだ原文（source_quote）、手描きは枠の下の span
                    // （取得中／失敗／0 件を分けて出す。失敗を「文字が無い」と伝えない）
                    const showSpans = !!r && r.id === selectedRegionId;
                    const isGhost = r?.originKind === "ghost";
                    const pageSpans = r ? spansByPage.get(r.drawnPage) : undefined;
                    const inRect =
                      showSpans && r && !isGhost && pageSpans !== undefined && pageSpans !== null
                        ? exampleValueFromSpans(pageSpans, r.drawnPage, r.bbox)
                        : undefined;
                    // 例示値が記入例語（サーバの判定）。選択していなくても出す ── 気付かせる
                    // ための注記なので、行を選んだときだけでは遅い
                    const placeholderWarn =
                      !!r && isPlaceholderExample(exampleForRegion(r), placeholderVerdicts);
                    return (
                      <div
                        key={d.rowId}
                        className={`rgn-row${d.include ? "" : " off"}${on ? " on" : ""}`}
                        onClick={() => setSelection({ kind: "row", rowId: d.rowId })}
                      >
                        {d.isNew ? (
                          // 新規行は「残す」を出さず × だけ（外す＝版に載らない＝消すと同じ。
                          // 編集モードの既存行のチェック「領域を付けるか」と意味が混ざる）
                          <button
                            className="rgn-rowx"
                            aria-label={`${d.label || "この項目"} を取り消す`}
                            title="この項目を取り消す（領域も消えます）"
                            onClick={(e) => {
                              e.stopPropagation();
                              removeNewRow(d.rowId);
                            }}
                          >
                            ×
                          </button>
                        ) : (
                          <input
                            type="checkbox"
                            checked={d.include}
                            onChange={(e) => setRowInclude(d.rowId, e.target.checked)}
                            aria-label={`${d.label} を含める`}
                          />
                        )}
                        <span className="rgn-names">
                          <input
                            value={d.label}
                            onChange={(e) => patchRow(d.rowId, { label: e.target.value })}
                            aria-label="表示名"
                            placeholder={d.isNew ? "表示名（例: 支払期日）" : undefined}
                            autoFocus={d.isNew && d.rowId === focusRowId}
                          />
                          <input
                            className="mono"
                            value={d.name}
                            onChange={(e) => patchRow(d.rowId, { name: e.target.value })}
                            aria-label="項目名"
                            placeholder={d.isNew ? "項目名（例: due_date）" : undefined}
                          />
                          {d.sample && (
                            <span className="sub" title={d.sample}>
                              今回の値: {d.sample}
                            </span>
                          )}
                        </span>
                        <select
                          value={d.type}
                          onChange={(e) => patchRow(d.rowId, { type: e.target.value })}
                          // 明細（表）の型は新プレビューでは変えない（columns を壊さない）
                          disabled={d.type === "table"}
                          aria-label="型"
                        >
                          {(d.isNew ? NEW_ROW_TYPE_OPTIONS : TYPE_OPTIONS).map(([v, l]) => (
                            <option key={v} value={v}>
                              {l}
                            </option>
                          ))}
                        </select>
                        <span className="sub">
                          {r ? (
                            <>
                              p.{r.drawnPage}{" "}
                              <button
                                className="btn sm ghost"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  deleteRegion(r.id);
                                }}
                              >
                                解除
                              </button>
                            </>
                          ) : (
                            "—"
                          )}
                        </span>
                        {showSpans && isGhost && (
                          <span className="sub rgn-rownote clip" title={r?.exampleValue ?? undefined}>
                            AI が読んだ原文: {r?.exampleValue ?? "（原文なし）"}
                          </span>
                        )}
                        {placeholderWarn && (
                          <span className="rgn-rownote rgn-rowwarn" role="note">
                            {PLACEHOLDER_ROW_NOTE}
                          </span>
                        )}
                        {(showSpans || placeholderWarn) && r && exampleForRegion(r) && !r.exampleCleared && (
                          // 例示値は帳票の値（個人名を含み得る）で、スキーマの版が残る限り残る。
                          // 気になる場合に作者が外せるようにする（§2.3）。記入例語の注記が出て
                          // いる行では、選択していなくても出す（注記が「例示値を消す」を案内する）
                          <button
                            className="btn sm ghost"
                            onClick={(e) => {
                              e.stopPropagation();
                              setRegions((rs) =>
                                rs.map((x) =>
                                  x.id === r.id ? { ...x, exampleValue: null, exampleCleared: true } : x,
                                ),
                              );
                            }}
                            title="この領域の例示値（前回ここにあった値）を保存しない"
                          >
                            例示値を消す
                          </button>
                        )}
                        {showSpans && !isGhost && (
                          <span className="sub rgn-rownote clip" title={inRect ?? undefined}>
                            枠に含まれる文字:{" "}
                            {pageSpans === undefined
                              ? "読み取り中…"
                              : pageSpans === null
                                ? "（文字を読み取れませんでした。行を選び直すと再取得します）"
                                : (inRect ?? "（この枠に文字は見つかりません）")}
                          </span>
                        )}
                        {d.isNew && (
                          // Part 2（位置のヒント）が既定 on になったので §1.5 の文言に差し替えた。
                          // 「位置のヒントが使われるのは今後の版から」の断りは、on の状態で
                          // 出すと嘘になるので復活させない
                          <span className="sub rgn-rownote" role="note">
                            読取領域は、この位置に何があるかのヒントとして抽出 AI に渡されます。
                            枠の下の文字が例示値として保存されます。
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>

                {/* 項目の追加（設計 v2 D1・D2・D3: 新規作成・編集の両モードで使える） */}
                <div className="rgn-add" role="group" aria-label="項目の追加">
                  <button
                    className={`btn sm${addingFromRegion ? " primary" : ""}`}
                    disabled={dimsUnavailable}
                    title={
                      dimsUnavailable ? "ページの寸法が取得できないため領域を引けません" : undefined
                    }
                    onClick={() => {
                      if (addingFromRegion) {
                        setAddingFromRegion(false);
                        return;
                      }
                      setDrawMode("include"); // 次のドラッグは読取領域（除外にはしない）
                      setAddingFromRegion(true);
                      setErr(null);
                    }}
                  >
                    {addingFromRegion ? "ドラッグを待っています…（中止）" : "＋ 領域から項目を追加"}
                  </button>
                  <button className="btn sm" onClick={addBlankRow}>
                    ＋ 項目を追加
                  </button>
                </div>

                <div className="rgn-ex">
                  <b>除外領域 {excludes.length > 0 && <span>（{excludes.length}）</span>}</b>
                  {excludes.length === 0 && (
                    <p className="sub" style={{ margin: 0 }}>
                      印影・ロゴなど読み取りたくない範囲があれば、上のトグルを「除外領域」に
                      してドラッグしてください。
                    </p>
                  )}
                  {excludes.map((r) => {
                    // この帳票でこの除外が実際に消す span（サーバの規則＝重なり 50%）。
                    // 別ページの応答は使わない（page_no が一致しなければ未取得と同じ）
                    const pageSpans = spansByPage.get(r.drawnPage);
                    const hit =
                      pageSpans && pageSpans.page_no === r.drawnPage
                        ? spansExcludedByRect(pageSpans.spans, r.bbox)
                        : [];
                    const preview = hit
                      .map((s) => s.text.trim())
                      .filter((t) => t.length > 0)
                      .join(" ");
                    return (
                    <div
                      key={r.id}
                      className={`rgn-exrow${selectedRegionId === r.id ? " on" : ""}`}
                      onClick={() => setSelection({ kind: "region", regionId: r.id })}
                    >
                      <input
                        value={r.label ?? ""}
                        placeholder="名前（任意）例: 社印"
                        onChange={(e) =>
                          setRegions((rs) =>
                            rs.map((x) => (x.id === r.id ? { ...x, label: e.target.value } : x)),
                          )
                        }
                        aria-label="除外領域の名前"
                      />
                      <select
                        value={r.page === null ? "all" : r.page === "last" ? "last" : "this"}
                        onChange={(e) =>
                          setRegions((rs) =>
                            rs.map((x) =>
                              x.id === r.id
                                ? {
                                    ...x,
                                    page:
                                      e.target.value === "all"
                                        ? null
                                        : e.target.value === "last"
                                          ? "last"
                                          : x.drawnPage,
                                  }
                                : x,
                            ),
                          )
                        }
                        aria-label="適用範囲"
                      >
                        <option value="this">p.{r.drawnPage} のみ</option>
                        <option value="all">全ページ</option>
                        <option value="last">最終ページ</option>
                      </select>
                      <button className="btn sm ghost" onClick={() => deleteRegion(r.id)}>
                        削除
                      </button>
                      <span className="sub rgn-rownote clip" title={preview || undefined}>
                        この領域内の文字:{" "}
                        {pageSpans === undefined
                          ? "読み取り中…"
                          : pageSpans === null
                            ? "（文字を読み取れませんでした）"
                            : hit.length === 0
                              ? "なし（この帳票では何も消えません）"
                              : `${hit.length} 件（p.${r.drawnPage}）` +
                                (preview ? `: ${truncateChars(preview, EXCLUDE_PREVIEW_MAX_LEN)}` : "")}
                      </span>
                    </div>
                    );
                  })}
                </div>

                {excludes.length > 0 && (
                  <div className="rgn-warn" role="note">
                    ⚠️ 除外領域は<b>同じ帳票種別のすべての帳票</b>に適用されます。レイアウトが
                    異なる取引先の帳票では、その位置にある実データも取り込まれません。対象
                    （印影等）の外接より大きくしすぎないでください。
                  </div>
                )}

                {/* 読取領域が現在どこまで効くのかを正直に書く（設計 v2 §1.5・既定 on）。
                    ヒントは抽出 AI に渡るが、この帳票でその位置に合う文字が無ければ
                    決定論のガードで自動的に捨てられ、従った／捨てたは検証画面に参考として
                    出るだけ。「レイアウト違いを検知できないので読取領域を確定することを
                    おすすめします」という以前の勧めは復活させない —— 領域が 1 行ずれると
                    正解が壊れ得る（2026-09-07 の実測）ので、引くことを勧める側に倒さない */}
                {(drawMode === "include" || includes.length > 0) && (
                  <p className="sub" style={{ margin: 0 }} role="note">
                    読取領域は、この位置に何があるかの<b>ヒント</b>として抽出 AI に渡されます。
                    この帳票でその位置に合う文字が無いときは自動的に使われず、その結果は
                    検証画面に<b>参考として</b>表示されます（「要確認」の判定には影響しません）。
                  </p>
                )}

                {activeRow && drawMode === "include" && !addingFromRegion && (
                  <p className="sub" style={{ margin: 0 }}>
                    選択中: <b>{activeRow.label || activeRow.name || "（名前なし）"}</b> —
                    画像上をドラッグするとこの項目の読取領域になります（既にある場合は
                    置き換わります）。
                  </p>
                )}

                {dimsUnavailable && (
                  <div className="rgn-warn" role="alert">
                    ⚠️ ページの寸法が取得できないため、この画面では領域を編集できません。
                    保存しても<b>既存の領域設定はそのまま保たれます</b>が、領域を変更したい
                    場合は画面を再読み込みしてからやり直してください。
                  </div>
                )}
                {preservedCount > 0 && (
                  <p className="sub" style={{ margin: 0 }}>
                    ほかに {preservedCount} 件の領域が、ページ寸法が未登録のため編集対象外です
                    （保存してもそのまま保持されます）。
                  </p>
                )}
                {err && (
                  <div className="rgn-err" role="alert">
                    {err}
                  </div>
                )}

                <div className="tpl-actions">
                  <button className="btn" onClick={onClose} disabled={busy}>
                    キャンセル
                  </button>
                  <button className="btn grad" onClick={save} disabled={busy}>
                    {busy
                      ? "保存中…"
                      : mode === "create"
                        ? "この抽出結果をテンプレート化"
                        : `新しい版として保存${prev ? `（v${prev.version + 1}）` : ""}`}
                  </button>
                </div>
              </>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
  // 開くのは利用者の操作の後（クライアント）だけなので document は必ずある。SSR で
  // 評価されたときだけ素のまま返す
  return typeof document === "undefined" ? overlay : createPortal(overlay, document.body);
}
