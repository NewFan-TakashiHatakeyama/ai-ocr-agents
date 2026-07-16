"""CLI の終了コード契約。CI ゲートはこれだけを見てブロックするので、
「劣化しているのに 0 を返す」ことが無いよう固定する。"""

from __future__ import annotations

import json
from pathlib import Path

from newfan_golden.cli import main

GOLD = [
    {
        "document_id": "gold_0001",
        "fields": [
            {"name": "合計金額", "value": "7003", "critical": True},
            {"name": "備考", "value": "至急"},
        ],
    }
]


def _jsonl(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    return path


def _run(tmp_path: Path, pred: list[dict[str, object]], **kw: Path) -> int:
    gold = _jsonl(tmp_path / "gold.jsonl", GOLD)
    p = _jsonl(tmp_path / "pred.jsonl", pred)
    argv = ["--gold", str(gold), "--pred", str(p)]
    for k, v in kw.items():
        argv += [f"--{k}", str(v)]
    return main(argv)


def test_全一致なら0で通る(tmp_path: Path) -> None:
    code = _run(
        tmp_path,
        [
            {
                "document_id": "gold_0001",
                "fields": [
                    {"name": "合計金額", "value": "7003"},
                    {"name": "備考", "value": "至急"},
                ],
            }
        ],
    )
    assert code == 0


def test_有害な補正があれば1でブロックする(tmp_path: Path) -> None:
    code = _run(
        tmp_path,
        [
            {
                "document_id": "gold_0001",
                "fields": [
                    {"name": "合計金額", "value": "7000", "corrected_from": "7003"},
                    {"name": "備考", "value": "至急"},
                ],
            }
        ],
    )
    assert code == 1


def test_予測が無い文書があれば2で落ちる(tmp_path: Path) -> None:
    # 黙って飛ばすと「抽出が落ちた文書」ほど指標から消えて数字が上がる
    assert _run(tmp_path, [{"document_id": "別の文書", "fields": []}]) == 2


def test_ベースライン比で劣化していれば1でブロックする(tmp_path: Path) -> None:
    baseline = tmp_path / "base.json"
    baseline.write_text(
        json.dumps({"exact_match": 1.0, "critical_exact_match": 1.0, "harmful_rate": 0.0}),
        encoding="utf-8",
    )
    code = _run(
        tmp_path,
        [{"document_id": "gold_0001", "fields": [{"name": "合計金額", "value": "9999"}]}],
        baseline=baseline,
    )
    assert code == 1


def test_out_に今回の指標を書き出す(tmp_path: Path) -> None:
    out = tmp_path / "metrics" / "current.json"
    code = _run(
        tmp_path,
        [
            {
                "document_id": "gold_0001",
                "fields": [
                    {"name": "合計金額", "value": "7003"},
                    {"name": "備考", "value": "至急"},
                ],
            }
        ],
        out=out,
    )
    assert code == 0
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["exact_match"] == 1.0
    assert saved["documents"] == 1
