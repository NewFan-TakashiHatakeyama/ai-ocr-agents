"""正解データの住所が ADR-0007 の正規形（norm_address_jp の不動点）かを検査する。

  uv run python golden/scripts/check_gold_addresses.py         # 検査。外れがあれば終了コード 1
  uv run python golden/scripts/check_gold_addresses.py --fix   # 正規形に書き換える

第 3 回計測（docs/design/region-measurement-2026-09-12.md）で住所の不正解の大半が
「郵便番号や『本社』を値に含めるか」という値の慣例の食い違いだった。慣例は正規化器
（newfan_normalizers.builtin.norm_address_jp）で 1 つに決め、正解データも同じ形で持つ。
ここで見るのは samples_ground_truth.json の ``*_address`` 項目で、正規化して値が変わる
もの＝慣例から外れているものを列挙する。

``--fix`` はラベラーの慣例（郵便番号なし・都道府県〜建物名/階・見出し語なし）を変えない。
変わるのは全角英数字・空白・番地の区切りだけである。正規化して**空になる**値（郵便番号や
見出し語しか無い）は慣例の問題ではなく正解値の誤りなので、--fix でも書き換えずに報告だけ
して終了コード 1 にする ── 人が画像を見て直すこと。

書き換えた後は golden/scripts/build_region_fixtures.py で派生フィクスチャ
（region_ab_s*.jsonl / *_goldspec.json）を作り直すこと。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from newfan_normalizers import NormContext
from newfan_normalizers.builtin import norm_address_jp

ROOT = Path(__file__).resolve().parents[2]
GT = ROOT / "golden" / "data" / "samples_ground_truth.json"
ADDRESS_SUFFIX = "_address"


def find_nonconforming(
    documents: dict[str, dict[str, Any]],
) -> list[tuple[str, str, str, str | None]]:
    """(ファイル名, 項目名, 現在の値, 正規形) を返す。正規形が None なら空になる値。"""
    ctx = NormContext()
    out: list[tuple[str, str, str, str | None]] = []
    for filename, rec in documents.items():
        for name, value in rec.get("fields", {}).items():
            if not name.endswith(ADDRESS_SUFFIX) or value is None:
                continue
            normalized = norm_address_jp(str(value), ctx).value
            if normalized != value:
                out.append((filename, name, str(value), normalized))
    return out


def _write_back(path: Path, raw: bytes, data: dict[str, Any]) -> None:
    """元ファイルの改行コードと末尾改行の有無を保って書き戻す（差分を値だけにする）。"""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if raw.endswith(b"\n"):
        text += "\n"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="正解データの住所が ADR-0007 の正規形か検査する")
    ap.add_argument("--gold", type=Path, default=GT)
    ap.add_argument("--fix", action="store_true", help="正規形に書き換える（空になる値は除く）")
    args = ap.parse_args(argv)

    raw = args.gold.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    rows = find_nonconforming(data["documents"])
    if not rows:
        print(f"[check_gold_addresses] 住所はすべて正規形です: {args.gold}")
        return 0

    empties = [r for r in rows if r[3] is None]
    fixable = [r for r in rows if r[3] is not None]
    for filename, name, before, after in rows:
        tag = "空になる（要目視）" if after is None else "正規形と違う"
        print(f"[check_gold_addresses] {tag}: {filename} {name}: {before!r} -> {after!r}")

    if args.fix and fixable:
        for filename, name, _before, after in fixable:
            data["documents"][filename]["fields"][name] = after
        _write_back(args.gold, raw, data)
        print(f"[check_gold_addresses] {len(fixable)} 件を正規形に書き換えました: {args.gold}")
        print("[check_gold_addresses] golden/scripts/build_region_fixtures.py で派生フィクスチャを作り直してください")
        return 1 if empties else 0
    print(f"[check_gold_addresses] 慣例から外れる住所 {len(rows)} 件（--fix で {len(fixable)} 件を揃えられます）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
