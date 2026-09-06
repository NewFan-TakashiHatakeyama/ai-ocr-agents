"""1 枚の帳票から読取領域を起こす（テンプレート化の再現）。

Phase 4 の A/B で使う領域を、**実際の運用と同じ手順**で作るためのツール。

前回（2026-09-06）の計測はここが弱かった。領域を「その帳票自身の検出位置」から
機械的に生成し、**同じ帳票に当てて**測っていたため、人が UI で引く矩形より遥かに
正確な領域を評価していた。実運用のテンプレートは:

    帳票 A でテンプレート化する → **別の帳票 B, C, D** に当たる

なので、A から起こした領域を B/C/D で評価しないと意味が無い。このツールは前半
（帳票 A から領域を起こす）を担う。

矩形の作り方は検証画面の「AIが見つけた位置をクリックで確定」と同じ:
検出 bbox に min(w,h) の 2% か 12px の大きい方を四方へ足す（web の padOf と同値）。
人はもっと雑に引くので、これは**人の矩形より正確な側**に寄る。その偏りは
結果の解釈で明示すること。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

from newfan_golden.region_ab import RegionAbError, _extract, _put_schema, _result, _upload

# web/components/TemplatizePreview.tsx の padOf と同じ余白の付け方
PAD_RATIO = 0.02
PAD_MIN_PX = 12.0


def pad_rect(bbox: list[float], page_w: int, page_h: int) -> list[float]:
    """検出 bbox（画素）に余白を足して正規化 rect にする。"""
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    pad = max(min(x2 - x1, y2 - y1) * PAD_RATIO, PAD_MIN_PX)
    return [
        max(0.0, (x1 - pad) / page_w),
        max(0.0, (y1 - pad) / page_h),
        min(1.0, (x2 + pad) / page_w),
        min(1.0, (y2 + pad) / page_h),
    ]


def derive(
    client: httpx.Client,
    image: Path,
    field_names: list[str],
    timeout_sec: float,
) -> dict[str, Any]:
    """帳票 1 枚を素抽出し、検出できた項目の領域を返す。

    領域が起こせるのは**その帳票で実際に検出できた項目だけ**。検出できなければ
    人も矩形を引けない（UI のゴーストが出ない）ので、ここで落ちるのは正しい。
    """
    schema = _put_schema(
        client,
        {
            "doc_type": f"derive_{image.stem}",
            "fields": [
                {"name": n, "label": n, "type": "string", "required": False, "critical": False}
                for n in field_names
            ],
            "create": False,
        },
    )
    document_id = _upload(client, image)
    try:
        status = _extract(client, document_id, str(schema["id"]), timeout_sec)
        if status != "succeeded":
            raise RegionAbError(f"{image.name}: 領域の起点にする抽出が {status}")
        res = _result(client, document_id)
        meta = client.get(f"/documents/{document_id}").json()
        dims = {p["page_no"]: (p.get("width"), p.get("height")) for p in meta.get("pages", [])}

        out: dict[str, Any] = {}
        missing: list[str] = []
        for f in res.get("fields", []):
            name = f.get("name")
            bbox = f.get("bbox")
            page = f.get("page") or 1
            w, h = dims.get(page, (None, None))
            if name not in field_names:
                continue
            if not bbox or not w or not h:
                missing.append(str(name))
                continue
            out[str(name)] = {"page": int(page), "rect": pad_rect(list(bbox), int(w), int(h))}
        for n in field_names:
            if n not in out and n not in missing:
                missing.append(n)
        return {"regions": out, "missing": missing, "pages": len(dims) or 1}
    finally:
        client.delete(f"/documents/{document_id}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="帳票 1 枚から読取領域を起こす（テンプレート化の再現）")
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument(
        "--spec",
        required=True,
        type=Path,
        help='{"<doc_type>": {"template_image": "path", "fields": ["issuer_name", ...]}} の JSON',
    )
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--timeout-sec", type=float, default=900.0)
    args = ap.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    headers = {"Authorization": f"Bearer {args.token}"}
    result: dict[str, Any] = {}
    report: dict[str, Any] = {}
    with httpx.Client(base_url=args.api.rstrip("/"), headers=headers, timeout=60.0) as client:
        for doc_type, cfg in spec.items():
            image = Path(cfg["template_image"])
            if not image.exists():
                raise RegionAbError(f"{doc_type}: 画像が見つかりません {image}")
            got = derive(client, image, list(cfg["fields"]), args.timeout_sec)
            result[doc_type] = got["regions"]
            report[doc_type] = {
                "template_image": str(image),
                "derived": sorted(got["regions"]),
                "missing": got["missing"],
                "source_page_count": got["pages"],
            }
            print(
                f"[derive] {doc_type}: {len(got['regions'])} 件 "
                f"（起こせなかった: {got['missing'] or 'なし'}）",
                flush=True,
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out.parent / (args.out.stem + "_report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
