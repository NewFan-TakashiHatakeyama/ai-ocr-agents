"""ゴールデンセット回帰の CLI（§14.2 の CI ゲート）。

  # 正解と予測を突き合わせて指標を出し、ゲート判定する
  uv run python -m newfan_golden.cli --gold golden/data/gold.jsonl \\
      --pred out/pred.jsonl --baseline golden/baselines/main.json

終了コードでブロックする（0=通過, 1=劣化/有害率超過, 2=入力不正）。CI はこれを見る。

pred.jsonl の形（1 行 = 1 ドキュメント）:
  {"document_id": "gold_0001",
   "fields": [{"name": "合計金額", "value": "7003",
               "review_status": "auto", "corrected_from": null}]}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from newfan_golden.dataset import GoldenFormatError, load_jsonl
from newfan_golden.metrics import PredField, Report, check_gate, evaluate


def _load_pred(path: Path) -> dict[str, list[PredField]]:
    out: dict[str, list[PredField]] = {}
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GoldenFormatError(f"{path}:{lineno} JSON として不正: {exc}") from exc
            out[str(obj["document_id"])] = [
                PredField(
                    name=str(f["name"]),
                    value=f.get("value"),
                    review_status=str(f.get("review_status", "auto")),
                    corrected_from=f.get("corrected_from"),
                )
                for f in obj.get("fields", [])
            ]
    return out


def _load_baseline(path: Optional[Path]) -> Optional[Report]:
    """前回の指標（to_dict 形式）から比較用 Report を組む。

    baseline は集計値しか持たないため、Report を再構成せず値だけを持つ薄い
    スタブにする（check_gate は集計プロパティしか見ない）。
    """
    if path is None:
        return None
    data = json.loads(path.read_text(encoding="utf-8"))

    class _Baseline(Report):
        @property
        def exact_match(self) -> float:
            return float(data["exact_match"])

        @property
        def critical_exact_match(self) -> float:
            return float(data["critical_exact_match"])

        @property
        def harmful_rate(self) -> float:
            return float(data["harmful_rate"])

    return _Baseline()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ゴールデンセット精度回帰（§14.2）")
    ap.add_argument("--gold", required=True, type=Path, help="正解 JSONL")
    ap.add_argument("--pred", required=True, type=Path, help="抽出結果 JSONL")
    ap.add_argument("--baseline", type=Path, help="比較する前回指標の JSON（省略時は有害率のみ判定）")
    ap.add_argument("--out", type=Path, help="今回の指標の書き出し先 JSON")
    args = ap.parse_args(argv)

    try:
        gold_docs = load_jsonl(args.gold)
        pred = _load_pred(args.pred)
    except (GoldenFormatError, OSError, KeyError) as exc:
        print(f"[golden] 入力が不正です: {exc}", file=sys.stderr)
        return 2

    missing = [d.document_id for d in gold_docs if d.document_id not in pred]
    if missing:
        # 予測が無い文書を黙って飛ばすと、実際は落ちているのに指標が上がって見える
        print(
            f"[golden] 予測が無い文書が {len(missing)} 件あります: {missing[:5]}",
            file=sys.stderr,
        )
        return 2

    report = evaluate(
        (d.document_id, d.fields, pred[d.document_id]) for d in gold_docs
    )
    gate = check_gate(report, _load_baseline(args.baseline))

    result = {**report.to_dict(), "gate": gate.to_dict()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    if not gate.passed:
        print("[golden] リリースゲート不通過:", file=sys.stderr)
        for r in gate.reasons:
            print(f"  - {r}", file=sys.stderr)
        return 1
    print("[golden] リリースゲート通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
