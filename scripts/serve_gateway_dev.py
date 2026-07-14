"""dev/QA 用 gateway 起動（In-Memory・seed 済み・CORS・固定JWT）。

web UI のブラウザ検証用。needs_review の帳票を1件 seed し、レビュア JWT を出力する。
ページ画像は SVG data URI（file:// のブラウザ制限を回避、bbox 座標系＝1000x1400）。

    uv run --with uvicorn python scripts/serve_gateway_dev.py
"""

from __future__ import annotations

import base64

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
from newfan_schemas import ExtractedField, ReviewStatus

SECRET = "dev-qa-secret-0123456789-abcdefghijklmnop"  # >= 32 bytes (HS256)

_SVG = """<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='1400'>
<rect width='1000' height='1400' fill='#ffffff' stroke='#cccccc'/>
<text x='60' y='70' font-size='40' font-family='sans-serif'>請求書</text>
<text x='100' y='105' font-size='24' font-family='sans-serif'>発行日 2026-05-01</text>
<text x='100' y='205' font-size='28' font-family='sans-serif'>御請求金額</text>
<text x='305' y='205' font-size='28' font-family='sans-serif'>¥128,OOO</text>
<text x='60' y='300' font-size='24' font-family='sans-serif'>株式会社サンプル御中</text>
</svg>"""


def _page_data_uri() -> str:
    b64 = base64.b64encode(_SVG.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _seed(repo: InMemoryRepository) -> None:
    repo.create_document(
        DocumentRecord(
            id="doc_demo",
            tenant_id="ten_1",
            storage_uri="s3://demo/original.png",
            original_name="invoice_demo.png",
            mime_type="image/png",
            page_count=1,
            doc_type="invoice",
            external_ref="ERP-1001",
            status="needs_review",
        ),
        [PageRecord(page_no=1, width=1000, height=1400, image_uri=_page_data_uri())],
    )
    repo.create_run(
        RunRecord(
            id="run_demo",
            tenant_id="ten_1",
            document_id="doc_demo",
            status="needs_review",
            result_version=1,
            engine_versions={"paddleocr": "3.7.0", "ocr": "PP-OCRv6_medium", "llm": "claude-opus-4-8"},
            fields=[
                ExtractedField(
                    name="total_amount",
                    label="合計金額(税込)",
                    value_raw="¥128,OOO",
                    value_normalized="128000",
                    confidence=0.72,
                    grounding_score=1.0,
                    page=1,
                    bbox=[300, 180, 432, 212],
                    source_quote="¥128,OOO",
                    span_ids=[42],
                    review_status=ReviewStatus.PENDING,
                ),
                ExtractedField(
                    name="invoice_date",
                    label="請求日",
                    value_raw="2026-05-01",
                    value_normalized="2026-05-01",
                    confidence=0.98,
                    grounding_score=1.0,
                    page=1,
                    bbox=[210, 88, 360, 112],
                    span_ids=[3],
                    review_status=ReviewStatus.AUTO,
                ),
                ExtractedField(
                    name="issuer_name",
                    label="取引先名",
                    value_raw="株式会社サンプル",
                    value_normalized="株式会社サンプル",
                    confidence=0.95,
                    grounding_score=1.0,
                    page=1,
                    bbox=[60, 280, 400, 308],
                    span_ids=[7],
                    review_status=ReviewStatus.AUTO,
                ),
            ],
            review_summary={"pending": 1, "auto": 2},
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
