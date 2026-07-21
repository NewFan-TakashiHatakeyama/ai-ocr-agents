"""ゴールデンセットを**実際に動いている系**に流して予測を採る（§14.2）。

  uv run python -m newfan_golden.collect --gold golden/data/gold.jsonl \\
      --api http://<alb>/v1 --token "$JWT" --out out/pred.jsonl

fixture 差し替えではなく本物の gateway → orchestrator → PaddleOCR → LLM を通す。
「本番昇格の条件」を測るのが目的なので、ここを Fake にすると測る意味が無くなる。

gold.jsonl の image_uri はローカルパス（またはローカルパスを指す file:// URI）を
指す必要がある。実帳票は顧客データなのでリポジトリには置かない（§14.2）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

import httpx

from newfan_golden.dataset import GoldenDoc, load_jsonl

TERMINAL_OK = {"succeeded", "completed", "done"}
TERMINAL_NG = {"failed", "error", "cancelled"}


class CollectError(RuntimeError):
    pass


def _local_path(image_uri: str) -> Path:
    parsed = urlparse(image_uri)
    if parsed.scheme in ("", "file"):
        raw = url2pathname(parsed.path) if parsed.scheme == "file" else image_uri
        p = Path(raw)
        if not p.exists():
            raise CollectError(f"画像が見つかりません: {image_uri}")
        return p
    # s3:// を勝手に落としに行かない。どのバケットを読むかは運用側の判断（§11）。
    raise CollectError(
        f"ローカルに無い画像です: {image_uri}\n"
        "  先に aws s3 cp で手元へ落とし、gold.jsonl の image_uri をそのパスにしてください。"
    )


def _upload(client: httpx.Client, doc: GoldenDoc) -> str:
    path = _local_path(doc.image_uri or "")
    files = {"file": (path.name, path.read_bytes(), "image/png")}
    data = {"doc_type": doc.doc_type} if doc.doc_type else {}
    r = client.post("/documents", files=files, data=data)
    r.raise_for_status()
    return str(r.json()["document_id"])


def _extract(client: httpx.Client, document_id: str, schema_id: str) -> str:
    body: dict[str, Any] = {"options": {}, "schema_id": schema_id}
    r = client.post(f"/documents/{document_id}/extract", json=body)
    r.raise_for_status()
    return str(r.json()["job_id"])


def _wait(client: httpx.Client, job_id: str, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}")
        r.raise_for_status()
        job = r.json()
        status = str(job["status"]).lower()
        if status in TERMINAL_NG:
            raise CollectError(f"ジョブ {job_id} が失敗しました: {job.get('error_code')}")
        if status in TERMINAL_OK:
            return
        # needs_review で止まるのは失敗ではない。HITL 待ちの状態も採点対象
        # （STP 率はまさに「止まった割合」を測る指標なので、ここで捨ててはいけない）。
        if status == "needs_review":
            return
        time.sleep(2.0)
    raise CollectError(f"ジョブ {job_id} が {timeout_sec}s 以内に終わりませんでした")


def _result(client: httpx.Client, document_id: str) -> list[dict[str, Any]]:
    r = client.get(f"/documents/{document_id}/result")
    r.raise_for_status()
    fields: list[dict[str, Any]] = []
    for f in r.json().get("fields", []):
        correction = f.get("correction") or {}
        fields.append(
            {
                "name": f["name"],
                # 採点は正規化後の値で行う（§14.2）
                "value": f.get("value_normalized"),
                "review_status": f.get("review_status", "auto"),
                # 補正が適用された場合のみ「補正前の値」が入る。有害率の分母/分子はこれで決まる。
                "corrected_from": correction.get("from") if correction.get("applied") else None,
            }
        )
    return fields


def collect(
    gold_path: Path,
    api: str,
    token: str,
    out: Path,
    *,
    schema_id: Optional[str] = None,
    timeout_sec: float = 600.0,
) -> int:
    docs = load_jsonl(gold_path)
    missing = [d.document_id for d in docs if not (d.schema_id or schema_id)]
    if missing:
        # schema_id 無しで走らせると load_context が空スキーマを返し、KIE が 1 項目も
        # 抽出しないまま「全項目 Recall 0」という無意味な結果が出る。先に止める。
        raise CollectError(
            f"schema_id が決まらない文書があります: {missing[:5]}\n"
            "  gold.jsonl に schema_id を書くか --schema-id を渡してください。"
        )

    rows: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=api.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    ) as client:
        for i, doc in enumerate(docs, start=1):
            print(f"[golden] ({i}/{len(docs)}) {doc.document_id} …", file=sys.stderr)
            document_id = _upload(client, doc)
            job_id = _extract(client, document_id, doc.schema_id or str(schema_id))
            _wait(client, job_id, timeout_sec)
            rows.append(
                {"document_id": doc.document_id, "fields": _result(client, document_id)}
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    print(f"[golden] {len(rows)} 件を {out} に書き出しました", file=sys.stderr)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ゴールデンセットを実系に流して予測を採る")
    ap.add_argument("--gold", required=True, type=Path)
    ap.add_argument("--api", required=True, help="gateway のベース URL（例: http://<alb>/v1）")
    ap.add_argument("--token", required=True, help="JWT")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--schema-id", help="固定したいスキーマ（省略時は doc_type から解決）")
    ap.add_argument("--timeout-sec", type=float, default=600.0)
    args = ap.parse_args(argv)
    try:
        return collect(
            args.gold,
            args.api,
            args.token,
            args.out,
            schema_id=args.schema_id,
            timeout_sec=args.timeout_sec,
        )
    except (CollectError, httpx.HTTPError) as exc:
        print(f"[golden] 収集に失敗しました: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
