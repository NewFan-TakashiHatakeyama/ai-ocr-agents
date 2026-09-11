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

import { useEffect, useMemo, useState } from "react";

import { RegionCanvas, type CanvasGhost, type CanvasRegion, type Px } from "@/components/RegionCanvas";
import { ApiError, api } from "@/lib/api";
import { guessFieldType } from "@/lib/fieldTypes";
// 保存 body の生成・検査・ゴースト解決は純粋関数（lib/templatize）に置き、
// ここでは state とイベントだけを持つ。単体テストはそちらにだけ付ける。
import {
  buildSaveBody,
  denormalize,
  padOf,
  resolveGhosts,
  resolvePage,
  validateDrafts,
  type DraftRow,
  type Preserved,
  type PreviewRegion,
} from "@/lib/templatize";
import { newUuid } from "@/lib/uuid";
import type { ExtractedField, PageDim, RegionRect } from "@/lib/types";

export const TYPE_OPTIONS = [
  ["string", "文字列"],
  ["date", "日付"],
  ["money_jpy", "金額(円)"],
  ["number", "数値"],
  ["tax_rate_jp", "税率"],
  ["jp_invoice_reg_no", "登録番号(T+13桁)"],
  ["jp_bank_account", "銀行口座"],
  // table 型は新規作成では選べないが、編集モードで既存の明細定義が来たときに
  // select が値を表示できずに壊れるため選択肢としては持つ（当該行は読み取り専用）。
  ["table", "明細（表）"],
] as const;

type Selection = { kind: "row"; rowId: string } | { kind: "region"; regionId: string } | null;

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
  onSaved: (r: {
    docType: string;
    schemaId: string;
    version: number;
    prevSchemaId: string | null;
  }) => void;
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

  const canvasRegions: CanvasRegion[] = regions
    .filter((r) => r.drawnPage === page)
    .map((r) => ({
      id: r.id,
      bbox: r.bbox,
      kind: r.kind,
      label:
        r.kind === "include"
          ? (drafts.find((d) => d.rowId === r.rowId)?.label ?? "読取")
          : (r.label || "除外"),
    }));

  const excludes = regions.filter((r) => r.kind === "exclude");
  const includes = regions.filter((r) => r.kind === "include");

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

  function addInclude(rowId: string, bbox: Px) {
    setRegions((rs) => [
      ...rs.filter((r) => !(r.kind === "include" && r.rowId === rowId)), // 置換
      { id: newUuid(), kind: "include", bbox, drawnPage: page, page, rowId },
    ]);
    patchRow(rowId, { include: true }); // 領域指定＝抽出したいの意思表示
    setErr(null);
  }

  function onDraw(bbox: Px) {
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
    addInclude(activeRowId, bbox);
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
    addInclude(d.rowId, [
      Math.max(0, x1 - p),
      Math.max(0, y1 - p),
      Math.min(W || x2 + p, x2 + p),
      Math.min(H || y2 + p, y2 + p),
    ]);
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
    const body = buildSaveBody({
      drafts,
      regions,
      preserved,
      dimsUnavailable,
      pageCount,
      pageDims: pages,
      mode,
    });

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

  return (
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
          <div className="rgn-modes" role="group" aria-label="描画モード">
            <button
              className={`btn sm${drawMode === "include" ? " primary" : ""}`}
              onClick={() => setDrawMode("include")}
            >
              読取領域
            </button>
            <button
              className={`btn sm${drawMode === "exclude" ? " primary" : ""}`}
              onClick={() => setDrawMode("exclude")}
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
              <span className="spacer" />
              <span className="sub">
                {drawMode === "include"
                  ? "破線＝AI が見つけた位置。クリックで確定、または空きをドラッグ"
                  : "読ませたくない範囲（印影・ロゴ等）をドラッグ"}
              </span>
            </div>
            <RegionCanvas
              documentId={documentId}
              pageNo={page}
              pageDim={dimsByPage.get(page)}
              regions={canvasRegions}
              ghosts={drawMode === "include" ? ghosts : []}
              mode={drawMode}
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
                    項目の追加・削除と必須／重要の設定はスキーマ管理画面で行います。ここでは
                    領域の指定と表示名・型の修正だけができます。
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
                    return (
                      <div
                        key={d.rowId}
                        className={`rgn-row${d.include ? "" : " off"}${on ? " on" : ""}`}
                        onClick={() => setSelection({ kind: "row", rowId: d.rowId })}
                      >
                        <input
                          type="checkbox"
                          checked={d.include}
                          onChange={(e) => setRowInclude(d.rowId, e.target.checked)}
                          aria-label={`${d.label} を含める`}
                        />
                        <span className="rgn-names">
                          <input
                            value={d.label}
                            onChange={(e) => patchRow(d.rowId, { label: e.target.value })}
                            aria-label="表示名"
                          />
                          <input
                            className="mono"
                            value={d.name}
                            onChange={(e) => patchRow(d.rowId, { name: e.target.value })}
                            aria-label="項目名"
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
                          {TYPE_OPTIONS.map(([v, l]) => (
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
                      </div>
                    );
                  })}
                </div>

                <div className="rgn-ex">
                  <b>除外領域 {excludes.length > 0 && <span>（{excludes.length}）</span>}</b>
                  {excludes.length === 0 && (
                    <p className="sub" style={{ margin: 0 }}>
                      印影・ロゴなど読み取りたくない範囲があれば、上のトグルを「除外領域」に
                      してドラッグしてください。
                    </p>
                  )}
                  {excludes.map((r) => (
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
                    </div>
                  ))}
                </div>

                {excludes.length > 0 && (
                  <div className="rgn-warn" role="note">
                    ⚠️ 除外領域は<b>同じ帳票種別のすべての帳票</b>に適用されます。レイアウトが
                    異なる取引先の帳票では、その位置にある実データも取り込まれません。対象
                    （印影等）の外接より大きくしすぎないでください。
                  </div>
                )}

                {/* 読取領域が現在どこまで効くのかを正直に書く。「レイアウト違いを
                    検知できないので読取領域を確定することをおすすめします」という
                    以前の勧めは復活させない —— 検証画面に参考として出るようになった
                    だけで、抽出結果そのものは変わらないため */}
                {(drawMode === "include" || includes.length > 0) && (
                  <p className="sub" style={{ margin: 0 }} role="note">
                    読取領域は<b>抽出結果を変えません</b>。指定しておくと、項目が想定と
                    違う位置で見つかったことの検知に使われ、その結果は検証画面に
                    <b>参考として</b>表示されます（値や「要確認」の判定には影響しません）。
                  </p>
                )}

                {activeRow && drawMode === "include" && (
                  <p className="sub" style={{ margin: 0 }}>
                    選択中: <b>{activeRow.label}</b> — 画像上をドラッグするとこの項目の
                    読取領域になります（既にある場合は置き換わります）。
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
}
