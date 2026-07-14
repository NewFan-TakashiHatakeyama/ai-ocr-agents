"""dev/QA 用 gateway 起動（In-Memory・seed 済み・CORS・固定JWT）。

web UI のブラウザ検証用。needs_review の帳票を1件 seed し、レビュア JWT を出力する。
ページ画像は SVG data URI（file:// のブラウザ制限を回避、bbox 座標系＝1000x1400）。

    uv run --with uvicorn python scripts/serve_gateway_dev.py
"""

from __future__ import annotations

import base64
from pathlib import Path

import jwt
import uvicorn

from newfan_gateway.admin import InMemoryAdminRepository
from newfan_gateway.app import create_app
from newfan_gateway.auth import InMemoryApiKeyStore
from newfan_gateway.config import Settings
from newfan_gateway.records import (
    DocumentRecord,
    MetricsSummary,
    PageRecord,
    RuleRecord,
    RunRecord,
    SchemaFieldDef,
    SchemaRecord,
)
from newfan_gateway.repository import InMemoryRepository
from newfan_schemas import ExtractedField, ReviewStatus, TableCell, TableResult

SECRET = "dev-qa-secret-0123456789-abcdefghijklmnop"  # >= 32 bytes (HS256)

# 実帳票 sample.png（御見積書, 793x1123）をそのままページ画像に使う。
# bbox 座標系は sample.png のピクセル空間（実 PP-StructureV3 の rec_polys 由来）。
_SAMPLE = Path(__file__).resolve().parents[1] / "sample.png"


def _page_data_uri() -> str:
    b64 = base64.b64encode(_SAMPLE.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _line_items_table() -> TableResult:
    # sample.png（御見積書, 793x1123）の明細表に概ね一致する列×行の座標でセル bbox を付与。
    cols = {"商品名": (40, 327), "数量": (420, 500), "単位": (500, 565), "単価": (565, 640), "金額": (640, 753)}
    data = [
        ("冷凍コロッケ", "250", "袋", "85", "21,250", 375, 396),
        ("冷凍ピザ", "180", "袋", "410", "85,600", 405, 425),
        ("カップラーメン醤油味", "200", "個", "100", "20,000", 428, 448),
        ("カップラーメン味噌味", "200", "個", "100", "20,000", 460, 480),
    ]
    rows = []
    for name, qty, unit, price, amount, y0, y1 in data:
        vals = {"商品名": name, "数量": qty, "単位": unit, "単価": price, "金額": amount}
        rows.append(
            {c: TableCell(value=vals[c], bbox=[x0, y0, x1, y1]) for c, (x0, x1) in cols.items()}
        )
    return TableResult(name="line_items", page=1, confidence=0.88, rows=rows)


def _seed(repo: InMemoryRepository) -> None:
    repo.create_document(
        DocumentRecord(
            id="doc_demo",
            tenant_id="ten_1",
            storage_uri="s3://demo/sample.png",
            original_name="御見積書_sample.png",
            mime_type="image/png",
            page_count=2,
            doc_type="quotation",
            external_ref="EST-00000101",
            status="needs_review",
        ),
        # p.1=構造OCR良好、p.2=品質ゲート NG で VL 補完（§5.4）。同じ画像を流用（デモ用）。
        [
            PageRecord(page_no=1, width=793, height=1123, image_uri=_page_data_uri()),
            PageRecord(page_no=2, width=793, height=1123, image_uri=_page_data_uri()),
        ],
    )
    repo.create_run(
        RunRecord(
            id="run_demo",
            tenant_id="ten_1",
            document_id="doc_demo",
            status="needs_review",
            result_version=1,
            engine_versions={"paddleocr": "3.7.0", "ocr": "PP-OCRv5_server", "llm": "claude-opus-4-8"},
            # 値/bbox は sample.png（御見積書, 793x1123）の実 PP-StructureV3 OCR 由来
            fields=[
                ExtractedField(
                    name="total_amount",
                    label="御見積合計金額",
                    value_raw="¥I36,998",  # OCR 誤読（I↔1）
                    value_normalized="I36998",
                    confidence=0.72,  # デモ用に要確認（実測は 0.93）
                    grounding_score=1.0,
                    page=1,
                    bbox=[235, 306, 320, 330],
                    source_quote="¥I36,998",
                    span_ids=[5],
                    review_status=ReviewStatus.PENDING,
                    correction={
                        "applied": False,
                        "needs_review": True,
                        "from": "I36998",
                        "to": "136,998",
                        "rationale": "混同ペア I↔1（先頭文字）。明細合計 ¥136,998 と一致（V-SUM）。",
                        "used_pairs": ["I↔1"],
                        "memory_refs": ["cor_01H8MQ"],
                    },
                ),
                ExtractedField(
                    name="issuer_name",
                    label="取引先名",
                    value_raw="ＡＡＡ食品株式会社",
                    value_normalized="ＡＡＡ食品株式会社",
                    confidence=0.99,
                    grounding_score=1.0,
                    page=1,
                    bbox=[41, 158, 166, 172],
                    span_ids=[2],
                    review_status=ReviewStatus.AUTO,
                ),
                ExtractedField(
                    name="quote_no",
                    label="見積番号",
                    value_raw="00000101",
                    value_normalized="00000101",
                    confidence=0.97,
                    grounding_score=1.0,
                    page=1,
                    bbox=[694, 155, 751, 172],
                    span_ids=[46],
                    review_status=ReviewStatus.AUTO,
                ),
                ExtractedField(
                    name="quote_date",
                    label="見積日",
                    value_raw="2014年04月01日",
                    value_normalized="2014-04-01",
                    confidence=1.0,
                    grounding_score=1.0,
                    page=1,
                    bbox=[653, 169, 749, 186],
                    span_ids=[49],
                    review_status=ReviewStatus.AUTO,
                ),
            ],
            tables=[_line_items_table()],
            review_summary={"pending": 1, "auto": 3},
            fallback_pages=[2],  # p.2 は VL 補完（バッジ/バナー露出のデモ, §5.4）
        )
    )


def _seed_admin() -> InMemoryAdminRepository:
    admin = InMemoryAdminRepository()
    admin.seed_schema(
        SchemaRecord(
            id="sch_inv", tenant_id="ten_1", doc_type="invoice", version=4,
            fields=[
                SchemaFieldDef(name="issuer_name", label="取引先名", type="string", required=True, critical=True),
                SchemaFieldDef(name="total_amount", label="合計金額（税込）", type="money_jpy", required=True, critical=True),
                SchemaFieldDef(name="registration_no", label="登録番号", type="jp_invoice_reg_no", critical=True),
                SchemaFieldDef(name="invoice_date", label="請求日", type="date", required=True),
            ],
        )
    )
    admin.seed_schema(
        SchemaRecord(id="sch_po", tenant_id="ten_1", doc_type="order", version=2,
                     fields=[SchemaFieldDef(name="order_no", label="注文番号", type="string", required=True)])
    )
    admin.seed_rule(RuleRecord(
        id="rul_01J8format", tenant_id="ten_1", doc_type="invoice", supplier_key="サンプル商事",
        field_name="伝票番号", rule_type="format", rule_json={"description": "伝票番号は先頭「7」の6桁", "pattern": "^7\\d{5}$", "on_violation": "needs_review"},
        status="draft", validation_report={"reproduction_rate": 1.0, "regressions": 0}, source_correction_ids=["cor_a", "cor_b", "cor_c", "cor_d", "cor_e", "cor_f"]))
    admin.seed_rule(RuleRecord(
        id="rul_01J8vocab", tenant_id="ten_1", supplier_key="サンプル商事", field_name="取引先名",
        rule_type="vocab_map", rule_json={"description": "「株式会社サンフル商事」→「サンプル商事」", "map": {"サンフル": "サンプル"}},
        status="active", validation_report={"reproduction_rate": 0.92, "regressions": 0}, source_correction_ids=["cor_g"] * 12))
    admin.seed_rule(RuleRecord(
        id="rul_01J6hint", tenant_id="ten_1", doc_type="delivery", supplier_key="□□印刷", field_name="日付",
        rule_type="llm_hint", rule_json={"description": "この取引先の日付は和暦表記"},
        status="validating", validation_report={"reproduction_rate": 0.6, "regressions": 1}, source_correction_ids=["cor_h", "cor_i", "cor_j"]))
    admin.set_metrics("ten_1", MetricsSummary(
        total_documents=2498, status_counts={"confirmed": 2048, "needs_review": 350, "failed": 100},
        stp_rate=0.824, corrections_total=1204, active_rules=1, pending_rules=1, memories_total=312))
    return admin


def main() -> None:
    repo = InMemoryRepository()
    _seed(repo)
    admin = _seed_admin()
    reviewer = jwt.encode({"sub": "reviewer1", "tenant_id": "ten_1", "role": "reviewer"}, SECRET, algorithm="HS256")
    admin_tok = jwt.encode({"sub": "admin1", "tenant_id": "ten_1", "role": "admin"}, SECRET, algorithm="HS256")
    print("=" * 60)
    print("DEV REVIEWER TOKEN:")
    print(reviewer)
    print("DEV ADMIN TOKEN (管理画面 SCR-04/05/06 用):")
    print(admin_tok)
    print("=" * 60)
    app = create_app(
        settings=Settings(jwt_secret=SECRET, cors_origins=["*"]),
        repo=repo,
        api_keys=InMemoryApiKeyStore({}),
        admin=admin,
    )
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
