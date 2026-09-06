"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { DeleteDocument } from "@/components/DeleteDocument";
import { DocViewer } from "@/components/DocViewer";
import { EditRegions } from "@/components/EditRegions";
import { ExtractStart } from "@/components/ExtractStart";
import { FieldPanel } from "@/components/FieldPanel";
import { RunWorkflow } from "@/components/RunWorkflow";
import { StatusChip } from "@/components/StatusChip";
import { TemplatizeSchema } from "@/components/TemplatizeSchema";
import { ApiError, api } from "@/lib/api";
import { sortFields } from "@/lib/fields";
import { useReviewStore } from "@/lib/store";
import { hasRole, usePrincipal } from "@/lib/principal";
import { useToasts } from "@/lib/toast";
import { fmtRemaining, useDocumentLock } from "@/lib/useDocumentLock";
import { useSchemaSaved, type SchemaSaved } from "@/lib/useSchemaSaved";

// SCR-03 検証画面（HITL, §8.2/§8.3）。本プロダクトの中核。全操作キーボード完結（§8.4）。
export default function ReviewPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["result", id],
    queryFn: () => api.getResult(id),
    // 4xx（未抽出の E1001 等）は確定エラー。リトライすると、バックオフ待機中に
    // 「読込中でも error でもない」状態になり抽出導線が一瞬フォールバックに化ける
    retry: (count, e) => {
      const st = (e as { status?: number } | null)?.status ?? 0;
      return st >= 500 && count < 2;
    },
  });
  // result が E1001 のとき、それが「未抽出」なのか「帳票ごと削除済み」なのかは
  // result だけでは区別できない（サーバはどちらも E1001）。帳票の存在を別途確かめる。
  const resultErrCode = (error as { code?: string } | null)?.code;
  const docExists = useQuery({
    queryKey: ["doc-exists", id],
    queryFn: () => api.getDocument(id).then(() => true).catch(() => false),
    enabled: resultErrCode === "E1001",
    retry: false,
    staleTime: 0,
    gcTime: 0,
  });
  const docGone = docExists.data === false;

  const { selectedField, selectedCell, edits, select, setEdit, clearEdits } = useReviewStore();
  const push = useToasts((s) => s.push);
  const router = useRouter();
  const qc = useQueryClient();

  // 削除後にこの画面へ戻ってこないよう、キャッシュを捨ててから一覧へ送る。
  // removeQueries を省くと、戻るボタンで消えた帳票の結果が一瞬表示される。
  const afterDelete = useCallback(() => {
    qc.removeQueries({ queryKey: ["result", id] });
    qc.removeQueries({ queryKey: ["classify", id] });
    qc.invalidateQueries({ queryKey: ["documents"] });
    qc.invalidateQueries({ queryKey: ["review-queue"] });
    qc.invalidateQueries({ queryKey: ["metrics"] });
    router.push("/documents");
  }, [qc, id, router]);
  const [busy, setBusy] = useState(false);
  const [page, setPage] = useState(1);
  // §8.2 ソフトロック: 他者が確認中なら readOnly（編集・確定を抑止）
  const { readOnly, remaining, holder } = useDocumentLock(id);
  const { me } = usePrincipal();
  // テンプレート化に成功したスキーマ。run の schema_id は作成後も null のままなので、
  // これが無いとバナーが「テンプレート化してください」を出し続け、成功したのか
  // 何も起きなかったのか画面から分からない（敵対的レビュー確定）。
  // **id も持つ**のは、作成直後の同じ帳票から領域編集に入れるようにするため
  // （doc_type だけだと write-once になる）。
  const [created, setCreated] = useState<{ docType: string; schemaId: string } | null>(null);
  const createdDocType = created?.docType ?? null;

  // テンプレート化した帳票と、そこから作ったスキーマの結び付きをブラウザに残す。
  // 作成しただけでは run.schema_id は null のままなので、リロードすると
  // 「この帳票から作った」手掛かりが消え、同じ帳票から領域を編集し直せなくなる
  // （設計の受け入れ条件「作成後に領域を編集できる」が満たせない）。
  // 参照のためだけの控えで、権限も正当性もサーバが判断する（スキーマが消えていれば
  // プリロードが失敗し、編集画面は保存を止める）。
  useEffect(() => {
    if (created !== null) return;
    try {
      const raw = window.localStorage.getItem(`nf_created_schema:${id}`);
      if (raw) setCreated(JSON.parse(raw));
    } catch {
      /* 保存不可・壊れた値は無視（無ければ編集ボタンが出ないだけ） */
    }
  }, [id, created]);

  const rememberCreated = useCallback(
    (r: { docType: string; schemaId: string }) => {
      setCreated(r);
      try {
        window.localStorage.setItem(`nf_created_schema:${id}`, JSON.stringify(r));
      } catch {
        /* プライベートモード等で保存できなくても致命ではない */
      }
    },
    [id],
  );

  // 保存後の処理（適用範囲の案内・再抽出の待ち受け・旧版ワークフローの警告）は
  // **この画面**が持つ。テンプレート化ボタンは保存に成功するとバナーごと消えるので、
  // そちら側に置くと再抽出のポーリングが即座に止まる（実機で踏んだ）。
  const afterSchemaSaved = useSchemaSaved({
    documentId: id,
    runStatus: data?.status ?? "",
    readOnly,
    onRefetch: () => {
      void refetch();
    },
  });
  const onTemplatized = useCallback(
    (r: SchemaSaved) => {
      rememberCreated({ docType: r.docType, schemaId: r.schemaId });
      afterSchemaSaved(r, true);
    },
    [rememberCreated, afterSchemaSaved],
  );
  const onRegionsEdited = useCallback(
    (r: SchemaSaved) => afterSchemaSaved(r, false),
    [afterSchemaSaved],
  );

  // **表示している run が入れ替わったら編集バッファを捨てる。**
  // edits はモジュールグローバルで、これまで clearEdits は確定成功時にしか呼ばれて
  // いなかった。再抽出は「画面を開いたまま run を差し替える」初めての導線なので、
  // 残しておくと旧 run に入力した値が 500ms デバウンスの自動保存で**新 run の
  // corrections として送られ、そのまま確定される**（新しい抽出で正しく取れた値を
  // 古い手入力が上書きする）。
  const shownRunId = data?.run_id ?? null;
  const lastRunRef = useRef<string | null>(null);
  useEffect(() => {
    if (!shownRunId) return;
    if (lastRunRef.current !== null && lastRunRef.current !== shownRunId) {
      clearEdits();
      select(null);
    }
    lastRunRef.current = shownRunId;
  }, [shownRunId, clearEdits, select]);

  const pending = useMemo(
    () => (data ? sortFields(data.fields).filter((f) => f.review_status === "pending") : []),
    [data],
  );
  // 領域の正規化・逆正規化にページ寸法が要る（単体取得だけが pages を埋める）。
  // 抽出結果が届いてから引く（未抽出の画面では不要）。
  const meta = useQuery({
    queryKey: ["doc-meta", id],
    queryFn: () => api.getDocument(id),
    enabled: Boolean(data),
    staleTime: 5 * 60_000,
  });
  const pageDims = useMemo(() => meta.data?.pages ?? [], [meta.data]);
  // ページ寸法が揃うまで領域の画面を開かせない。開けてしまうと、プリロードの
  // 非同期コールバックが**マウント時の空の寸法表**を掴んだまま既存領域を復元できず、
  // その後に寸法が届いても「警告は消えているのに領域は見えない」画面になる。
  // 保全ロジックが最終防波堤として残るが、そもそも入口を閉じる方が確実。
  const pageDimsReady = pageDims.length > 0;

  const fallbackPages = useMemo(() => new Set(data?.fallback_pages ?? []), [data]);
  const pages = useMemo(() => {
    const s = new Set<number>();
    data?.fields.forEach((f) => f.bbox && s.add(f.page ?? 1));
    fallbackPages.forEach((p) => s.add(p)); // VL 補完ページ（抽出フィールドが無くてもタブを出す）
    // 除外領域だけがあるページ（印影ページ等）もタブに出す。出さないと
    // 「画像にあるのに結果に無い」理由を見に行けない
    data?.applied_exclude_regions?.forEach((r) => s.add(r.page_no));
    return s.size ? [...s].sort((a, b) => a - b) : [1];
  }, [data, fallbackPages]);

  useEffect(() => {
    // 選択フィールド/セルのページを表示
    const f = data?.fields.find((x) => x.name === selectedField);
    if (f?.page) setPage(f.page);
    else if (selectedCell) setPage(selectedCell.page);
  }, [selectedField, selectedCell, data]);

  const submit = useCallback(
    async (force = false) => {
      if (!data) return;
      const pendingCount = Number(data.review_summary?.pending ?? 0);
      if (pendingCount > 0 && !force) {
        if (!window.confirm(`未確認が ${pendingCount} 件あります。確定してよいですか？`)) return;
      }
      setBusy(true);
      try {
        const items = Object.entries(edits).map(([field_name, corrected_value]) => ({
          field_name,
          original_value: data.fields.find((f) => f.name === field_name)?.value_normalized ?? null,
          corrected_value,
        }));
        if (items.length > 0) {
          await api.postCorrections(data.document_id, data.run_id, data.result_version, items);
        }
        await api.confirm(data.document_id, data.run_id);
        clearEdits();
        push({ kind: "ok", message: "確定しました。会計システムへ連携します（完了は通知されます）。" });
        refetch();
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) {
          push({
            kind: "warn",
            message: "他のメンバーが先に更新しました。最新を読み込みます。",
            action: { label: "最新を表示", onClick: () => refetch() },
          });
        } else {
          push({ kind: "err", message: `確定に失敗しました（${(e as Error).message}）。時間をおいて再試行してください。` });
        }
      } finally {
        setBusy(false);
      }
    },
    [data, edits, clearEdits, push, refetch],
  );

  // §8.4 キーボード完結
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      // テンプレート化ダイアログ表示中は画面ショートカットを全て止める。
      // 特に Ctrl/⌘+Enter は typing 判定より先に評価されるため、doc_type 入力中に
      // 押すと帳票の確定（会計連携）が発火してしまう（敵対的レビュー確定）。
      if (document.querySelector(".tpl-overlay")) return;
      const typing = ev.target instanceof HTMLInputElement || ev.target instanceof HTMLTextAreaElement;
      if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter") {
        ev.preventDefault();
        if (!readOnly) void submit();
        return;
      }
      if (typing) {
        if (ev.key === "Escape") (ev.target as HTMLInputElement).blur();
        return;
      }
      if (pending.length === 0) return;
      const idx = pending.findIndex((f) => f.name === selectedField);
      if (ev.key === "n" || ev.key === "Enter") {
        ev.preventDefault();
        select(pending[(idx + 1 + pending.length) % pending.length].name);
      } else if (ev.key === "p") {
        ev.preventDefault();
        select(pending[(idx - 1 + pending.length) % pending.length].name);
      } else if (ev.key === "e" && idx < 0 && pending[0]) {
        select(pending[0].name);
      } else if ((ev.key === "1" || ev.key === "2") && idx >= 0 && !readOnly) {
        // 候補採択: 1=OCR原値 / 2=LLM補正案（§8.3 CharDiffPopover）
        const f = pending[idx];
        const corr = (f.correction ?? {}) as { to?: string };
        const val = ev.key === "1" ? f.value_raw ?? "" : corr.to ?? f.value_raw ?? "";
        setEdit(f.name, val);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pending, selectedField, select, setEdit, submit, readOnly]);

  // §8.3 No.4 autosave: 編集を 500ms デバウンスで POST /corrections（version 付き楽観ロック）
  const [saveState, setSaveState] = useState<"idle" | "saving" | "saved">("idle");
  useEffect(() => {
    if (!data || readOnly || Object.keys(edits).length === 0) return;
    const handle = setTimeout(async () => {
      const items = Object.entries(edits).map(([field_name, corrected_value]) => ({
        field_name,
        original_value: data.fields.find((f) => f.name === field_name)?.value_normalized ?? null,
        corrected_value,
      }));
      setSaveState("saving");
      try {
        await api.postCorrections(data.document_id, data.run_id, data.result_version, items);
        setSaveState("saved");
      } catch (e) {
        setSaveState("idle");
        if (e instanceof ApiError && e.status === 409) {
          push({
            kind: "warn",
            message: "他のメンバーが先に更新しました。最新を読み込みます。",
            action: { label: "最新を表示", onClick: () => refetch() },
          });
        }
        // その他のエラーは静かに（確定時に再送される）
      }
    }, 500);
    return () => clearTimeout(handle);
  }, [edits, data, push, refetch, readOnly]);

  if (isPending) return <div className="page">読み込み中…</div>;
  // まだ抽出していない（run 無し = E1001）なら行き止まりにせず「抽出を開始」導線を出す。
  // instanceof は dev のモジュール重複で false になり得るため code を直接見る（duck typing）
  const errCode = (error as { code?: string } | null)?.code;
  if (errCode === "E1001" && docGone) {
    // 帳票そのものが消えている。サーバは「帳票が無い」と「run が無い」を同じ
    // E1001 で返すので、result だけ見ると削除済みの帳票が「まだ抽出していません」
    // として復活して見える（削除 → 戻る、で必ず踏む）。
    return (
      <div className="main">
        <div className="rv-head">
          <Link href="/documents" className="btn sm ghost">
            ← 一覧
          </Link>
        </div>
        <div className="empty">
          <div className="emoji">🗑️</div>
          <h3>この帳票は削除されました</h3>
          <p>抽出結果・原本ファイルとも残っていません。</p>
          <Link href="/documents" className="btn grad" style={{ marginTop: 10 }}>
            ドキュメント一覧へ
          </Link>
        </div>
      </div>
    );
  }
  if (errCode === "E1001" && docExists.isPending) {
    // 「未抽出」と「削除済み」の判定が付くまでは出し分けない（一瞬 ExtractStart が
    // 見えてから消えるより、読み込み中のままの方が誤解が無い）
    return <div className="page">読み込み中…</div>;
  }
  if (errCode === "E1001") {
    return (
      <div className="main">
        <div className="rv-head">
          <Link href="/documents" className="btn sm ghost">
            ← 一覧
          </Link>
          <span className="docname" style={{ marginLeft: 8 }}>{id}</span>
          {/* 未抽出の帳票（誤アップロード等）こそ消したい。ここに導線が無いと
              一覧に戻らないと消せない */}
          <span style={{ marginLeft: "auto" }}>
            <DeleteDocument documentId={id} onDeleted={afterDelete} />
          </span>
        </div>
        <ExtractStart documentId={id} onDone={() => refetch()} />
      </div>
    );
  }
  if (error || !data) return <div className="page">結果の取得に失敗しました。</div>;

  const regionStats = data.region_stats ?? {};
  const excludedTotal =
    (regionStats.excluded_spans ?? 0) +
    (regionStats.excluded_cells ?? 0) +
    (regionStats.excluded_rows ?? 0);
  // 除外領域を置いたページは、レイアウト解析結果（本文テキスト）を**ページ単位で**
  // 抽出 AI の入力から落としている。座標を持たないので部分的に消せないため。
  // 印影のように文字を持たない対象だと span もセルも 0 件で、この事実だけが残る。
  // 件数 0 でバッジを出さないと、KIE の主要入力が 1 つ欠けたことが誰にも見えない。
  const mdDropped = regionStats.markdown_dropped_pages ?? [];
  const showExcludeBadge = excludedTotal > 0 || mdDropped.length > 0;
  // 編集対象の doc_type は「サーバが解決した doc_type → このセッションで作成した
  // doc_type」の順。どちらも取れないときは編集させない（空のプリロードで保存すると
  // 既存の定義を空の新版で上書きしてしまう）。
  const editDocType = data.schema_doc_type ?? createdDocType;
  // 入口条件は page.tsx:290 の「=== null の厳密比較」と同じ方針。旧 gateway 混在窓で
  // schema_id キー自体が来ない（undefined）ときは出さない側に倒す。
  const canEditRegions =
    hasRole(me.role, "admin") &&
    !readOnly &&
    pageDimsReady &&
    Boolean(editDocType) &&
    (typeof data.schema_id === "string" || created !== null);

  const auto = Number(data.review_summary?.auto ?? data.fields.filter((f) => f.review_status !== "pending").length);
  const pend = Number(data.review_summary?.pending ?? pending.length);
  const total = auto + pend || 1;

  return (
    <div className="main">
      <div className="rv-head">
        <Link href="/documents?tab=queue" className="btn sm ghost">
          ← 一覧
        </Link>
        <div>
          <b className="rv-title">{data.document_id}</b>
          <span className="sub"> · run {data.run_id.slice(0, 12)}</span>
        </div>
        <StatusChip status={data.status} />
        {saveState !== "idle" && (
          <span className="sub" aria-live="polite">
            {saveState === "saving" ? "保存中…" : "✓ 保存済み"}
          </span>
        )}
        <span className="spacer" />
        <div className="rv-prog">
          <span className="seg" aria-hidden>
            <i style={{ width: `${(auto / total) * 100}%`, background: "var(--green)" }} />
            <i style={{ width: `${(pend / total) * 100}%`, background: "var(--amber)" }} />
          </span>
          確定 {auto} ·{" "}
          <b style={{ color: pend ? "var(--amber-ink)" : "var(--green)" }}>要確認 {pend}</b>
        </div>
        {showExcludeBadge && (
          <span
            className="rv-exbadge"
            title={
              "除外領域の設定により取り込まれなかった内容です。" +
              (mdDropped.length
                ? `\np.${mdDropped.join("・p.")} は本文テキスト全体を抽出 AI の入力から除いています（領域は座標を持つが本文は持たないため、ページ単位でしか除けません）。`
                : "")
            }
          >
            🚫 除外領域:{" "}
            {excludedTotal > 0 ? (
              <>
                span {regionStats.excluded_spans ?? 0} 件
                {(regionStats.excluded_cells ?? 0) > 0 &&
                  ` / セル ${regionStats.excluded_cells} 件`}
                {(regionStats.excluded_rows ?? 0) > 0 && ` / 行 ${regionStats.excluded_rows} 件`}
                を未取込
              </>
            ) : (
              <>p.{mdDropped.join("・p.")} の本文を未使用</>
            )}
          </span>
        )}
        {canEditRegions && (
          <EditRegions
            documentId={id}
            fields={data.fields}
            pages={pageDims}
            docType={editDocType!}
            onSaved={onRegionsEdited}
          />
        )}
        <RunWorkflow documentId={data.document_id} />
        {/* 他者が確認中は消させない（サーバ側も 409 で弾くが、押せない方が親切） */}
        <DeleteDocument documentId={id} disabled={readOnly} onDeleted={afterDelete} />
        <button
          className="btn primary"
          disabled={busy || readOnly || data.status === "confirmed"}
          onClick={() => submit()}
        >
          {busy ? "処理中…" : pend > 0 ? `確定（要確認 ${pend}件）` : "確定して連携へ"}
        </button>
      </div>

      {/* === null の厳密比較にする: 旧 gateway（schema_id キー無し）と新 web の
          混在窓では undefined になり、緩い比較だとスキーマ指定済みの帳票まで
          「自動発見」バナーが出る（敵対的レビュー確定）。undefined なら出さない側に倒す */}
      {data.schema_id === null && createdDocType === null && (
        <div className="tpl-banner" role="status">
          {/* 「自動抽出できます」とは書かない: 実際には次回もアップロード後に人が
              「抽出を開始」を押す必要があり、テンプレートが自動で選ばれるのは
              種別を判定できた場合だけ（gateway routers.py の classify_document）。
              能力を先取りした文言は「効くと言われて効かない」体験になる */}
          🧩 この抽出は<b>スキーマなしの自動発見</b>です。項目を確認して
          テンプレート化すると、次回の同種帳票でこの定義を選んで抽出できます。
          <span className="spacer" />
          {hasRole(me.role, "admin") ? (
            <TemplatizeSchema
              documentId={id}
              fields={data.fields}
              pages={pageDims}
              disabled={!pageDimsReady}
              onSaved={onTemplatized}
            />
          ) : (
            <span className="sub">（テンプレート化は管理者が実行できます）</span>
          )}
        </div>
      )}
      {createdDocType !== null && (
        <div className="tpl-banner" role="status">
          {/* 「取込時に種別を指定すると」とはまだ書かない: API は doc_type を受けるが
              web のアップロード導線が渡していないため、画面上にその操作が存在しない */}
          ✅ スキーマ「<b>{createdDocType}</b>」を作成しました。この帳票の値はこのまま
          確認・確定できます。次回の同種帳票は、抽出画面でこのスキーマを選べます
          （ファイル名に種別が含まれていれば既定で選ばれます）。
        </div>
      )}
      {readOnly && (
        <div className="rv-lock" role="status" aria-live="polite">
          🔒 {holder ?? "他のメンバー"} が確認中です（残り {fmtRemaining(remaining)}）。
          このドキュメントは読み取り専用です。
        </div>
      )}

      <div className="rv-body">
        <div className="viewer">
          <div className="v-tools">
            {pages.map((p) => (
              <button
                key={p}
                className={`pagetab${p === page ? " on" : ""}${fallbackPages.has(p) ? " vl" : ""}`}
                onClick={() => setPage(p)}
                title={fallbackPages.has(p) ? "VL補完ページ（構造/OCR品質が低い）" : undefined}
              >
                p.{p}
                {fallbackPages.has(p) && <span className="src vl">VL</span>}
              </button>
            ))}
            <span className="spacer" />
            <span className="sub">前処理後PNG＝座標系の正（DD-01）</span>
          </div>
          {fallbackPages.has(page) && (
            <div className="vl-banner" role="status">
              🅥 このページは <b>VL補完</b>（構造/OCR品質が低いため画像モデルで抽出）。
              由来 <span className="src vl">VL</span> の値は grounding 上限 0.7・特に確認が必要です（DD-09）。
            </div>
          )}
          <DocViewer
            documentId={id}
            fields={data.fields}
            pageNo={page}
            excludeRegions={data.applied_exclude_regions ?? []}
          />
        </div>
        <FieldPanel fields={data.fields} tables={data.tables} readOnly={readOnly} />
      </div>

      <div className="rv-foot">
        <span>
          <span className="kbd">n</span>/<span className="kbd">p</span> 次/前の要確認
        </span>
        <span>
          <span className="kbd">Enter</span> 次へ
        </span>
        <span>
          <span className="kbd">e</span> 編集
        </span>
        <span>
          <span className="kbd">⌘</span>＋<span className="kbd">Enter</span> 確定
        </span>
        <span className="spacer" />
        <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
          由来:
          <span className="src ocr">OCR</span>
          <span className="src llm">LLM</span>
          <span className="src rule">ルール</span>
          <span className="src human">人手</span>
          <span className="src vl">VL</span>
        </span>
      </div>
    </div>
  );
}
