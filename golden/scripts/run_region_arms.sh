#!/usr/bin/env bash
# 読取領域ヒントの A/B 計測（設計 region-field-add-and-hint-v2 §3 の 3 アーム）を、
# 手順どおりに 1 本で回す。実測記録 region-measurement-2026-09-12.md「再実行の手順」から呼ぶ。
#
#   golden/scripts/run_region_arms.sh --api http://localhost:8000/v1 --token "$JWT" \
#       --out out/phase4 [--trials-s1 5] [--structure http://localhost:8081]
#
# アームごと（S3 → S2 → S1 の順。ゲートを決める S3 を先に）に:
#   1. region_from_gold: 正解値の位置から領域を起こす（例示値も一緒に出る）。
#      出力 <out>/<arm>_goldregions.json があれば飛ばす（領域は決定論なので回し直しても同じ）
#   2. 領域と位置依存項目（*_positional.json の _positional）を 1 つの JSON に合わせる。
#      S1 は example_value / origin を落とす（同じ帳票の例示値は必ず example_present になり
#      答えを教えるのと同じ。設計 §3）
#   3. region_ab: 対照（領域なし）と介入（領域あり）を **同じ試行で交互に** 回す。
#      交互に回すのが手順そのもの（時間帯によるモデル側の揺れを両条件へ均等に散らす）。
#      片方のアームだけを回し直す使い方（--resume で対照を丸ごと再利用）は region_ab が止める。
#      <out>/<arm>_ab.json が既にあれば、それを --resume に渡して **欠けた対だけ** 埋める
#      （LLM 側の失敗で捨てた試行の埋め直し。元は <arm>_ab_prefill.json に残す）。
#
# 試行数: S3 / S2 は 5（設計 §3）、S1 は --trials-s1（既定 5。第 3 回で 2 試行は検出力不足と
# 分かった）。前提: worker のヒントが有効（既定 on。REGION_KIE_HINTS を off にしていないこと）、
# フィクスチャ（golden/data/region_ab_s{1,2,3}.jsonl と *_goldspec.json / *_positional.json）は
# golden/scripts/build_region_fixtures.py で作ってあること。
# Python は $PYTHON があればそれ、無ければ .venv のもの。
set -euo pipefail

API=""
TOKEN=""
OUT=""
TRIALS_S1=5
STRUCTURE="http://localhost:8081"
usage() {
  echo "使い方: $0 --api URL --token JWT --out DIR [--trials-s1 N] [--structure URL]" >&2
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --api) API="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --trials-s1) TRIALS_S1="$2"; shift 2;;
    --structure) STRUCTURE="$2"; shift 2;;
    -h|--help) usage;;
    *) echo "不明な引数: $1" >&2; usage;;
  esac
done
[ -n "$API" ] && [ -n "$TOKEN" ] && [ -n "$OUT" ] || usage

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
else PY=python; fi
export PYTHONPATH=golden/src
export PYTHONIOENCODING=utf-8
mkdir -p "$OUT"

gold_regions() {  # $1=goldspec  $2=out
  if [ -f "$2" ]; then
    echo "[skip] 領域は取得済み: $2"
  else
    "$PY" -m newfan_golden.region_from_gold --api "$API" --token "$TOKEN" \
      --structure "$STRUCTURE" --spec "$1" --out "$2"
  fi
}

merge() {  # $1=regions  $2=positional  $3=out  $4=strip_example(0/1)
  "$PY" - "$1" "$2" "$3" "$4" <<'PYEOF'
import json, pathlib, sys
regions, positional, out, strip = sys.argv[1:5]
r = json.loads(pathlib.Path(regions).read_text(encoding="utf-8"))
p = json.loads(pathlib.Path(positional).read_text(encoding="utf-8"))
if strip == "1":
    # S1: 同じ帳票の例示値は答えを教えるのと同じなので落とす（origin も一緒に）
    for fields in r.values():
        for reg in fields.values():
            reg.pop("example_value", None)
            reg.pop("origin", None)
r["_positional"] = p["_positional"]
pathlib.Path(out).write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
n = sum(1 for dt, fs in r.items() if dt != "_positional" for reg in fs.values() if reg.get("example_value"))
print("merged ->", out, "例示値あり", n)
PYEOF
}

for ARM in s3 s2 s1; do
  case "$ARM" in
    s3) TRIALS=5; STRIP=0; LABEL="S3: 別雛形テンプレートの害（12 件 × 5 試行、例示値あり）";;
    s2) TRIALS=5; STRIP=0; LABEL="S2: 同じ雛形への転用（5 件 × 5 試行、例示値あり）";;
    s1) TRIALS="$TRIALS_S1"; STRIP=1; LABEL="S1: 機構が効くか（21 枚 × $TRIALS_S1 試行、例示値なし）";;
  esac
  echo "=============== $LABEL ==============="
  gold_regions "golden/data/region_ab_${ARM}_goldspec.json" "$OUT/${ARM}_goldregions.json"
  merge "$OUT/${ARM}_goldregions.json" "golden/data/region_ab_${ARM}_positional.json" \
    "$OUT/${ARM}_regions_full.json" "$STRIP"
  RESUME=()
  if [ -f "$OUT/${ARM}_ab.json" ]; then
    # 前回の出力がある: 欠けた対だけ埋める（片方のアームを丸ごと再利用する形なら region_ab が止める）
    cp "$OUT/${ARM}_ab.json" "$OUT/${ARM}_ab_prefill.json"
    RESUME=(--resume "$OUT/${ARM}_ab_prefill.json")
    echo "[resume] 前回の出力から欠けた対だけ回す: $OUT/${ARM}_ab_prefill.json"
  fi
  "$PY" -m newfan_golden.region_ab --gold "golden/data/region_ab_${ARM}.jsonl" \
    --regions "$OUT/${ARM}_regions_full.json" --api "$API" --token "$TOKEN" \
    --trials "$TRIALS" --out "$OUT/${ARM}_ab.json" ${RESUME[@]+"${RESUME[@]}"}
done

echo "=============== 完了 ==============="
echo "ゲート判定: $PY golden/scripts/eval_gates_v2.py --s1 $OUT/s1_ab.json --s2 $OUT/s2_ab.json --s3 $OUT/s3_ab.json --md $OUT/gates.md"
