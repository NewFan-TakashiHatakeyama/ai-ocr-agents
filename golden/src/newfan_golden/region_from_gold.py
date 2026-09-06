"""正解値の**紙面上の位置**から読取領域を起こす（人が矩形を引く操作の再現）。

なぜ必要か。領域を「抽出 AI が返した bbox」から起こすと、**AI が取り違えた項目では
領域まで間違う**。実測でこれが起きた: ビズリフォーム A4 の 3 枚のうち 2 枚で、
抽出が発行元として紙面**左**（実際は宛先）を返し、そこから起こした領域が
「発行元＝左」というテンプレートになった。そのテンプレートで A/B を回しても
測れるのは「間違ったヒントは害になるか」であって、「正しいヒントは効くか」ではない。

実運用では、テンプレート化は検証画面（値を人が直したあと）から行い、UI では
AI が見つけた位置をクリックで採るだけでなく**自分でドラッグして引ける**。
つまり人のテンプレートは、AI が外した項目でも正しい位置を指す。

そこでこのツールは:

  1. ページ画像（前処理後 PNG＝座標系の正, DD-01）を structure-svc に投げて span を得る
  2. **正解値の文字列を span 列から探す**（1 span で足りなければ読み順で連結して探す）
  3. 見つかった span 群の外接に余白を足して正規化 rect にする

見つからない場合は領域を作らない。OCR が文字を拾えていないなら、どんなヒントを
渡しても抽出は当てられないので、そこは計測から外すのが正しい。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any, Optional

import httpx

from newfan_paddle_client.client import PaddleServingClient
from newfan_paddle_client.spans import build_spans
from newfan_golden.region_ab import RegionAbError, _upload
from newfan_golden.region_from_doc import pad_rect

# 連結して探す最大 span 数。住所は 2〜3 span に割れることが多い。
# 大きくすると「たまたま繋がって一致した」偽陽性が増える。
MAX_JOIN = 4


def key(s: Optional[str]) -> str:
    """位置探索用のキー。表記の揺れを落として突き合わせる。"""
    if s is None:
        return ""
    t = unicodedata.normalize("NFKC", str(s))
    t = "".join(t.split())
    for ch in (",", "￥", "¥", "円", "-", "−", "－", "ー", "‐", "･", "・", "様", "御中", "殿"):
        t = t.replace(ch, "")
    return t.casefold()


def locate(spans: list[Any], want: str) -> tuple[Optional[list[float]], int]:
    """正解値を含む最小の span 連続列の外接を返す。

    戻り値は (bbox, 候補数)。候補数が多い項目（同じ金額が複数箇所に出る等）は
    人でも一意に引けないので、呼び出し側で記録して解釈に使う。
    """
    target = key(want)
    if not target:
        return None, 0
    hits: list[tuple[int, list[float]]] = []
    for i in range(len(spans)):
        head = len(key(spans[i].text))
        joined = ""
        for n in range(MAX_JOIN):
            if i + n >= len(spans):
                break
            joined += key(spans[i + n].text)
            at = joined.find(target)
            # **先頭の span から一致が始まること**を要求する。これが無いと、
            # 関係の無い span から連結を始めた組が候補として数えられ、
            # 「同じ値が何箇所にあるか」の数が水増しされる。
            if at >= 0 and at < head:
                boxes = [spans[i + j].bbox for j in range(n + 1)]
                hits.append(
                    (
                        n + 1,
                        [
                            float(min(b[0] for b in boxes)),
                            float(min(b[1] for b in boxes)),
                            float(max(b[2] for b in boxes)),
                            float(max(b[3] for b in boxes)),
                        ],
                    )
                )
                break
            if len(joined) > len(target) * 3:
                break  # 伸ばしても届かない
    if not hits:
        return None, 0
    hits.sort(key=lambda h: h[0])  # 連結数が少ない＝素直に一致したものを採る
    return hits[0][1], len(hits)


def page_spans(
    gw: httpx.Client, paddle: PaddleServingClient, document_id: str, page_no: int
) -> list[Any]:
    """前処理後のページ画像を structure-svc に投げて span 列を得る。"""
    signed = gw.get(f"/documents/{document_id}/pages/{page_no}/image").json()["url"]
    img = httpx.get(signed, timeout=120.0)
    img.raise_for_status()
    resp = paddle.layout_parsing(base64.b64encode(img.content).decode("ascii"))
    out: list[Any] = []
    for elem in resp.layout_parsing_results:
        out.extend(build_spans(elem.pruned_result, page=page_no))
    return out


def derive_for_doc(
    gw: httpx.Client,
    paddle: PaddleServingClient,
    image: Path,
    gold_fields: dict[str, str],
) -> dict[str, Any]:
    document_id = _upload(gw, image)
    try:
        meta = gw.get(f"/documents/{document_id}").json()
        dims = {p["page_no"]: (p.get("width"), p.get("height")) for p in meta.get("pages", [])}
        regions: dict[str, Any] = {}
        report: dict[str, Any] = {"not_found": [], "ambiguous": {}}
        for page_no in sorted(dims):
            w, h = dims[page_no]
            if not w or not h:
                continue
            spans = page_spans(gw, paddle, document_id, page_no)
            for name, want in gold_fields.items():
                if name in regions or not want:
                    continue
                bbox, n = locate(spans, want)
                if bbox is None:
                    continue
                regions[name] = {"page": page_no, "rect": pad_rect(bbox, int(w), int(h))}
                if n > 1:
                    report["ambiguous"][name] = n
        report["not_found"] = [n for n, v in gold_fields.items() if v and n not in regions]
        report["pages"] = len(dims)
        return {"regions": regions, "report": report}
    finally:
        gw.delete(f"/documents/{document_id}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="正解値の位置から読取領域を起こす")
    ap.add_argument("--api", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--structure", default="http://localhost:8081")
    ap.add_argument(
        "--spec",
        required=True,
        type=Path,
        help='{"<doc_type>": {"template_image": "path", "gold": {"field": "value"}}} の JSON',
    )
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    regions: dict[str, Any] = {}
    reports: dict[str, Any] = {}
    headers = {"Authorization": f"Bearer {args.token}"}
    with httpx.Client(base_url=args.api.rstrip("/"), headers=headers, timeout=180.0) as gw:
        with PaddleServingClient(args.structure, timeout=600.0) as paddle:
            for doc_type, cfg in spec.items():
                image = Path(cfg["template_image"])
                if not image.exists():
                    raise RegionAbError(f"{doc_type}: 画像が見つかりません {image}")
                got = derive_for_doc(gw, paddle, image, dict(cfg["gold"]))
                regions[doc_type] = got["regions"]
                reports[doc_type] = {"template_image": str(image), **got["report"]}
                print(
                    f"[gold-region] {doc_type}: {len(got['regions'])} 件 "
                    f"（見つからず: {got['report']['not_found'] or 'なし'}"
                    f"{' / 複数箇所: ' + str(got['report']['ambiguous']) if got['report']['ambiguous'] else ''}）",
                    flush=True,
                )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(regions, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out.parent / (args.out.stem + "_report.json")).write_text(
        json.dumps(reports, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
