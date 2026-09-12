#!/usr/bin/env bash
# 読取領域ヒントの A/B 計測（設計 region-field-add-and-hint-v2 §3 の 3 アーム）を、
# 手順どおりに 1 本で回す。実測記録 region-measurement-2026-09-12.md「再実行の手順」から呼ぶ。
#
#   golden/scripts/run_region_arms.sh --api http://localhost:8000/v1 --token "$JWT" \
#       --out out/phase4 [--trials-s1 5] [--structure http://localhost:8081] [--resume] [--arms s3,s2,s1]
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
#
# 前回の出力（<out>/<arm>_ab.json）がある --out には、--resume を付けたときだけ回す:
#   - --resume あり: 欠けた (帳票, 試行, アーム) があるアームは、出力を region_ab --resume に
#     渡して **欠けた対だけ** 埋める（LLM 側の失敗で捨てた試行の埋め直し、試行数の延長。
#     元は <arm>_ab_prefill.json に残す）。欠けた対が無いアームは **回さず、書き直さない**。
#   - --resume なし: 止める（exit 2）。完全な出力を region_ab --resume に渡すと 1 件も抽出せずに
#     同じ行を書き直し、前回の結果が新しい計測に化ける（mtime だけ新しくなる。出力には
#     どの worker / プロンプトで回したかが残らないので、後から見分けられない）。
#     コードやプロンプトを変えて測り直すなら別の --out にする。
#
# --arms: 回すアームを絞る（既定 s3,s2,s1 の全部）。変更が**決定論で特定のアームにしか届かない**
# と示せるとき（例: 例示値の語彙で落とすガード。S1 は例示値を落として回すので届かない）に、
# そのアームだけ測り直すのに使う。プロンプトの変更は波及するので絞らない（設計 §3）。
# 絞ったときのゲート判定は、回さなかったアームに前回の出力を渡す（記録に「どの回の出力か」を残す）。
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
RESUME_MODE=0
ARMS="s3,s2,s1"
usage() {
  echo "使い方: $0 --api URL --token JWT --out DIR [--trials-s1 N] [--structure URL] [--resume] [--arms s3,s2,s1]" >&2
  exit 2
}
while [ $# -gt 0 ]; do
  case "$1" in
    --api) API="$2"; shift 2;;
    --token) TOKEN="$2"; shift 2;;
    --out) OUT="$2"; shift 2;;
    --trials-s1) TRIALS_S1="$2"; shift 2;;
    --structure) STRUCTURE="$2"; shift 2;;
    --resume) RESUME_MODE=1; shift;;
    --arms) ARMS="$2"; shift 2;;
    -h|--help) usage;;
    *) echo "不明な引数: $1" >&2; usage;;
  esac
done
[ -n "$API" ] && [ -n "$TOKEN" ] && [ -n "$OUT" ] || usage
ARM_LIST="${ARMS//,/ }"
for ARM in $ARM_LIST; do
  case "$ARM" in s1|s2|s3) ;; *) echo "不明なアーム: $ARM（s1 / s2 / s3）" >&2; usage;; esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then PY=.venv/bin/python
else PY=python; fi
export PYTHONPATH=golden/src
export PYTHONIOENCODING=utf-8
mkdir -p "$OUT"

# 前回の出力がある --out に --resume なしで回さない（前回の結果を新しい計測として書き直さないため）
EXISTING=""
for ARM in $ARM_LIST; do
  [ -f "$OUT/${ARM}_ab.json" ] && EXISTING="$EXISTING $OUT/${ARM}_ab.json"
done
if [ -n "$EXISTING" ] && [ "$RESUME_MODE" -eq 0 ]; then
  echo "前回の出力があります:$EXISTING" >&2
  echo "欠けた対を埋めるなら --resume を付けてください。コードやプロンプトを変えて測り直すなら" \
       "別の --out にしてください（前回の出力を新しい計測として書き直さないため）。" >&2
  exit 2
fi

gold_regions() {  # $1=goldspec  $2=out
  if [ -f "$2" ]; then
    echo "[skip] 領域は取得済み: $2"
  else
    "$PY" -m newfan_golden.region_from_gold --api "$API" --token "$TOKEN" \
      --structure "$STRUCTURE" --spec "$1" --out "$2"
  fi
}

missing_pairs() {  # $1=gold jsonl  $2=前回の出力  $3=trials → 欠けた (帳票, 試行, アーム) の数
  "$PY" - "$1" "$2" "$3" <<'PYEOF'
import json, pathlib, sys
from newfan_golden.dataset import load_jsonl
from newfan_golden.region_ab import missing_pairs
gold, prev, trials = sys.argv[1:4]
resume = json.loads(pathlib.Path(prev).read_text(encoding="utf-8"))
print(len(missing_pairs(load_jsonl(pathlib.Path(gold)), int(trials), resume)))
PYEOF
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

for ARM in $ARM_LIST; do
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
    # --resume（上で確かめてある）: 欠けた対が無ければこのアームは回さない（書き直すと前回の
    # 結果が新しい計測に化ける）。あれば欠けた対だけ埋める（片方のアームを丸ごと再利用する
    # 形なら region_ab が止める）
    MISSING="$(missing_pairs "golden/data/region_ab_${ARM}.jsonl" "$OUT/${ARM}_ab.json" "$TRIALS")"
    if [ "$MISSING" -eq 0 ]; then
      echo "[skip] 前回の出力に欠けた対はありません（測り直すなら別の --out）: $OUT/${ARM}_ab.json"
      continue
    fi
    cp "$OUT/${ARM}_ab.json" "$OUT/${ARM}_ab_prefill.json"
    RESUME=(--resume "$OUT/${ARM}_ab_prefill.json")
    echo "[resume] 前回の出力から欠けた対 $MISSING 件だけ回す: $OUT/${ARM}_ab_prefill.json"
  fi
  "$PY" -m newfan_golden.region_ab --gold "golden/data/region_ab_${ARM}.jsonl" \
    --regions "$OUT/${ARM}_regions_full.json" --api "$API" --token "$TOKEN" \
    --trials "$TRIALS" --out "$OUT/${ARM}_ab.json" ${RESUME[@]+"${RESUME[@]}"}
done

echo "=============== 完了 ==============="
echo "ゲート判定: $PY golden/scripts/eval_gates_v2.py --s1 $OUT/s1_ab.json --s2 $OUT/s2_ab.json --s3 $OUT/s3_ab.json --md $OUT/gates.md"
