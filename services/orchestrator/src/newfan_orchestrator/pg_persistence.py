# mypy: ignore-errors
"""PgContextStore（本番）: PostgreSQL 実装（§7 / §4.3）。

runtime 依存（sqlalchemy + psycopg）。RLS 用に SET LOCAL app.tenant_id を発行する
（テーブル所有者ロールは既定で RLS を bypass。本番は非所有ロール＋FORCE RLS を推奨）。
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import create_engine, text

from newfan_orchestrator.persistence import LoadedContext

# schema 未割当（テンプレートレス既定）の run に渡す placeholder。
# FieldSchema.doc_type は str 必須のため None を入れると deterministic_normalize が
# ValidationError で落ちる（実コンテナの E2E で検出）。nodes 側の既定と同じ "" に揃える。
EMPTY_SCHEMA: dict = {"doc_type": "", "fields": []}


class PgContextStore:
    def __init__(self, dsn: str) -> None:
        self._engine = create_engine(dsn, future=True)

    def load_context(self, tenant_id: str, run_id: str):
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            run = c.execute(
                text("SELECT document_id, schema_id FROM extraction_runs WHERE id = :r"),
                {"r": run_id},
            ).first()
            if run is None:
                return None
            document_id, schema_id = run
            schema = dict(EMPTY_SCHEMA)
            exclude_regions: list = []
            source_page_count = None
            if schema_id is not None:
                srow = c.execute(
                    text(
                        "SELECT doc_type, fields, exclude_regions, source_page_count "
                        "FROM field_schemas WHERE id = :s"
                    ),
                    {"s": schema_id},
                ).first()
                if srow is not None:
                    # exclude_regions / source_page_count は **schema dict に入れない**
                    # （§4.6）。schema は make_kie_extract がそのまま json.dumps で
                    # プロンプトへ埋めるため、座標を混ぜると LLM 出力が変わる。
                    schema = {"doc_type": srow[0], "fields": srow[1]}
                    exclude_regions = list(srow[2] or [])
                    source_page_count = srow[3]
            pages = [
                {"page_no": r[0], "image_uri": r[1], "width": r[2], "height": r[3]}
                for r in c.execute(
                    text(
                        "SELECT page_no, image_uri, width, height FROM pages "
                        "WHERE document_id = :d ORDER BY page_no"
                    ),
                    {"d": document_id},
                )
            ]
            return LoadedContext(
                document_id=document_id,
                schema=schema,
                pages=pages,
                exclude_regions=exclude_regions,
                source_page_count=source_page_count,
            )

    def run_exists(self, tenant_id, run_id) -> bool:
        # documents を JOIN する。帳票が消えると extraction_runs は FK CASCADE で
        # 消えるので JOIN 無しでも足りるが、明示しておくと「なぜ run が消えるのか」が
        # 読んで分かる。
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            r = c.execute(
                text(
                    "SELECT 1 FROM extraction_runs r"
                    " JOIN documents d ON d.id = r.document_id"
                    " WHERE r.id = :r AND r.tenant_id = :t"
                ),
                {"r": run_id, "t": tenant_id},
            ).first()
        return r is not None

    def save_result(
        self,
        tenant_id,
        run_id,
        *,
        fields,
        tables,
        review_items,
        status,
        fallback_pages=None,
        region_stats=None,
    ) -> None:
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            for f in fields:
                c.execute(
                    text(
                        "INSERT INTO extraction_fields "
                        "(id, tenant_id, run_id, field_name, value_raw, value_normalized, "
                        " final_value, confidence, grounding_score, page_no, bbox, source_quote, "
                        # correction/validation を落とすと SCR-03 の LLM補正候補・検証結果が
                        # 本番で一度も出ない（DDL には列があるのに INSERT に無かった）
                        " span_ids, review_status, correction, validation, label) "
                        "VALUES (:id,:t,:r,:fn,:vr,:vn,:fv,:cf,:gs,:pg, CAST(:bb AS jsonb), :sq, "
                        " CAST(:si AS jsonb), :rs, CAST(:co AS jsonb), CAST(:va AS jsonb), :lb) "
                        "ON CONFLICT (run_id, field_name) DO UPDATE SET "
                        # 再配信の再実行では行全体を新抽出で書き直す。一部の列だけ更新すると
                        # 「correction は新抽出・value_raw/bbox は旧抽出」のキメラ行になり、
                        # SCR-03 が実在しない対立候補を提示する（敵対的レビュー確定）
                        " value_raw = EXCLUDED.value_raw, "
                        " value_normalized = EXCLUDED.value_normalized, "
                        " final_value = EXCLUDED.final_value, "
                        " confidence = EXCLUDED.confidence, "
                        " grounding_score = EXCLUDED.grounding_score, "
                        " page_no = EXCLUDED.page_no, "
                        " bbox = EXCLUDED.bbox, "
                        " source_quote = EXCLUDED.source_quote, "
                        " span_ids = EXCLUDED.span_ids, "
                        " correction = EXCLUDED.correction, "
                        " validation = EXCLUDED.validation, "
                        " label = EXCLUDED.label, "
                        " review_status = EXCLUDED.review_status "
                        # 人手確定（corrected/approved）を機械の再抽出（pending/auto）で
                        # 巻き戻さない。XACK 前クラッシュ→確定→再配信、の順で人手作業が
                        # 消える経路を閉じる（敵対的レビュー確定）。finalize の再保存
                        # （EXCLUDED も corrected/approved）は通る
                        "WHERE NOT (extraction_fields.review_status IN ('corrected','approved')"
                        " AND EXCLUDED.review_status IN ('pending','auto'))"
                    ),
                    {
                        "id": f"fld_{uuid.uuid4().hex[:20]}",
                        "t": tenant_id,
                        "r": run_id,
                        "fn": f.name,
                        "vr": f.value_raw,
                        "vn": f.value_normalized,
                        "fv": f.value_normalized,
                        "cf": f.confidence,
                        "gs": f.grounding_score,
                        "pg": f.page,
                        "bb": json.dumps(f.bbox) if f.bbox is not None else None,
                        "sq": f.source_quote,
                        "si": json.dumps(f.span_ids),
                        "rs": f.review_status.value,
                        "lb": f.label,
                        "co": json.dumps(f.correction, ensure_ascii=False) if f.correction else None,
                        "va": json.dumps(f.validation, ensure_ascii=False) if f.validation else None,
                    },
                )
            # 再実行で今回の抽出に無い名前の旧行を掃除する。スキーマ指定なら名前集合は
            # 固定だが、スキーマレス自動発見（ADR-0006）は名前を LLM が毎回発明するため、
            # 再配信の再実行で名前が揺れると旧発見行が幽霊フィールドとして結果に並ぶ
            # （UPSERT は「同名の上書き」しかせず、消えた名前を消さない）。
            # 人手確定（corrected/approved）は上の WHERE ガードと同じ理由で残す。
            if fields:
                c.execute(
                    text(
                        "DELETE FROM extraction_fields WHERE run_id = :r"
                        " AND NOT (field_name = ANY(:names))"
                        " AND review_status NOT IN ('corrected','approved')"
                    ),
                    {"r": run_id, "names": [f.name for f in fields]},
                )
            # 明細テーブル（構造由来, §5.3）を extraction_tables に永続化。冪等のため run 分を洗替。
            c.execute(text("DELETE FROM extraction_tables WHERE run_id = :r"), {"r": run_id})
            for t in tables:
                rows_json = [
                    {
                        col: {"value": cell.value, "span_ids": cell.span_ids, "bbox": cell.bbox}
                        for col, cell in row.items()
                    }
                    for row in t.rows
                ]
                c.execute(
                    text(
                        "INSERT INTO extraction_tables "
                        "(id, tenant_id, run_id, name, page_no, structure_html, rows, confidence) "
                        "VALUES (:id,:t,:r,:nm,:pg,:html, CAST(:rows AS jsonb), :cf)"
                    ),
                    {
                        "id": f"tbl_{uuid.uuid4().hex[:20]}",
                        "t": tenant_id,
                        "r": run_id,
                        "nm": t.name,
                        "pg": t.page,
                        "html": t.structure_html,
                        "rows": json.dumps(rows_json, ensure_ascii=False),
                        "cf": t.confidence,
                    },
                )
            # fallback_pages（VL 露出用, §5.4）は metrics JSONB へマージ（既存キーは保持）。
            # confirmed の run を再配信の再実行が needs_review 等へ巻き戻さない
            # （フィールドの人手確定ガードと同じ経路対策。confirmed→confirmed の
            #   再保存は冪等に通す）
            run_upd = c.execute(
                text(
                    "UPDATE extraction_runs SET status = :st, finished_at = now(), "
                    "metrics = (COALESCE(metrics, '{}'::jsonb) || CAST(:m AS jsonb))"
                    # region_stats が空なら **キーごと落とす**。単に書かないだけ
                    # だと、同じ run を再実行したときに前回の mismatch_fields が
                    # 残り、検証画面が解消済みの所見を出し続ける（state 側は
                    # ocr_nodes / nodes が毎回 pop して対策済みだが DB 側は
                    # 素通しだった）。全 run に region:null を書く案は採らない
                    # ——「領域を使っていない run は完全に no-op」という後方互換を
                    # DB 側で破ってしまう。
                    + ("" if region_stats else " - 'region'") +
                    " WHERE id = :r AND NOT (status = 'confirmed' AND :st <> 'confirmed')"
                ),
                {
                    "st": status,
                    "r": run_id,
                    # region は「除外で消した件数」。needs_review 保存の時点で載って
                    # いないと、レビュー中だけ検証画面の除外バッジが出ない（§5.4）。
                    "m": json.dumps(
                        {
                            "fallback_pages": sorted(set(fallback_pages or [])),
                            **({"region": region_stats} if region_stats else {}),
                        }
                    ),
                },
            )
            if run_upd.rowcount:
                # ドキュメント状態は run が実際に遷移した時だけ追従させる
                c.execute(
                    text(
                        "UPDATE documents SET status = :st, updated_at = now() "
                        "WHERE id = (SELECT document_id FROM extraction_runs WHERE id = :r)"
                    ),
                    {"st": status, "r": run_id},
                )

    def add_run_metrics(self, tenant_id, run_id, patch) -> None:
        # 加算で追記（resume ジョブでも積み上がる）。metrics 全体の上書きはしない
        from sqlalchemy import text

        sets = []
        params: dict = {"t": tenant_id, "r": run_id}
        for i, (k, v) in enumerate(patch.items()):
            if not k.replace("_", "").isalnum():
                continue  # 識別子でないキーは無視（SQL へ埋めるため）
            sets.append(
                f"'{k}', COALESCE((metrics->>'{k}')::numeric, 0) + :v{i}"
            )
            params[f"v{i}"] = v
        if not sets:
            return
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.execute(
                text(
                    "UPDATE extraction_runs SET metrics = metrics ||"
                    f" jsonb_build_object({', '.join(sets)})"
                    " WHERE tenant_id=:t AND id=:r"
                ),
                params,
            )

    def mark_run_failed(self, tenant_id, run_id) -> None:
        # processing のまま失敗した run を failed へ。needs_review/confirmed（再配信成功後）は
        # 触らない（WHERE status='processing'）ので自己回復する。document も failed にして
        # 一覧で分かるようにする。これが無いと has_active_run が掴んで再抽出が E1005 で塞がる。
        from sqlalchemy import text

        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.execute(
                text(
                    "UPDATE extraction_runs SET status='failed', finished_at=now()"
                    " WHERE id=:r AND tenant_id=:t AND status='processing'"
                ),
                {"r": run_id, "t": tenant_id},
            )
            c.execute(
                text(
                    "UPDATE documents SET status='failed', updated_at=now()"
                    " WHERE tenant_id=:t AND id=(SELECT document_id FROM extraction_runs"
                    "   WHERE id=:r) AND status='processing'"
                ),
                {"r": run_id, "t": tenant_id},
            )

    def set_job_status(self, tenant_id, job_id, status, *, error_code=None) -> None:
        # §6.3 の契約はクライアントが GET /jobs/{id} を polling して終了を待つこと。
        # ここを書かないと jobs.status は queued のままで、ジョブが成功しても
        # クライアントはタイムアウトするまで待ち続ける。
        with self._engine.begin() as c:
            c.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            c.execute(
                text(
                    "UPDATE jobs SET status = :st, error_code = :ec, "
                    " started_at = COALESCE(started_at, CASE WHEN :st = 'running' "
                    "                       THEN now() ELSE started_at END), "
                    " finished_at = CASE WHEN :st IN ('succeeded','failed','dead') "
                    "                    THEN now() ELSE finished_at END, "
                    " attempt = attempt + CASE WHEN :st = 'running' THEN 1 ELSE 0 END "
                    "WHERE id = :j AND tenant_id = :t"
                ),
                {"st": status, "ec": error_code, "j": job_id, "t": tenant_id},
            )
