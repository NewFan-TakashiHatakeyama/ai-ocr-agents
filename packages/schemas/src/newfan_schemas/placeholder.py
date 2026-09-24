"""記入例語（placeholder_example）の判定（設計 region-field-add-and-hint-v2 §2.5）。

例示値（``RegionRect.example_value``）が値ではなく**記入欄の説明**（「自社名」「住所1」
「〇〇株式会社」「YYYY/MM/DD」）かを決定論で判定する。純関数のみ（re / unicodedata だけ）。

定義をここ（newfan_schemas）に置くのは、次の 2 か所が**同じ規則**で判定するため:

- orchestrator: KIE の前の事前ガード（``region_hint.prevalidate`` が
  ``placeholder_example`` でヒントを落とす）と、除外領域の降格の材料選び
  （``region_mask.layout_probe``）
- gateway: テンプレート化／領域編集画面の保存時の警告（``PUT /schemas`` の応答）と
  判定 API（``POST /schemas/example-values/check``）

規則が 2 つに分かれると「画面では警告が出ないのに実行時は落ちる」（または逆）という、
原因の追いにくい差になる。web で TypeScript に書き直さず、画面はこの判定を API で
引く（設計 region-template-editor §6）。PR #16（第 5 回計測）で orchestrator の
``region_hint`` に入れたものを、判定を変えずにここへ移した。``region_hint`` からは
re-export しているので、既存の import はそのまま使える。

判定の前に ``sanitize_example_value``（制御文字・改行の削除と 200 字の上限）を通すのは
呼ぶ側の責務（保存される値・プロンプトへ載る値と同じ文字列で判定するため）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# 記入例の帳票（sample13）から作ったテンプレートは、例示値が「自社名(ロゴや社判も登録
# できます)」「東京都千代田区住所1住所2ビル名等」「得意先の会社名」のように**値ではなく
# 記入欄の説明**になる。種類判定は unknown なので kind_conflict は効かず、別雛形に当てると
# 候補（別の項目の値）にそのまま従う（第 4 回計測 S3 の sample2 ← sample13、−6）。
# 例示値の語彙で決定論に落とす（落としても対照に戻るだけ。迷ったら落とす）。
#
# 規則は 4 つ。順に当てて、どれかに当たれば記入例語:
# 1. 強い印（_PLACEHOLDER_STRONG）: 値にも見出しにも現れない語・形。含めば落とす
#    （「住所1」「ビル名等」「登録できます」「記入例」「例）」「YYYY/MM/DD」「{{name}}」
#    「@example.com」「Lorem ipsum」）
# 2. 見出し語だけ（_PLACEHOLDER_LABELS ＋ _PLACEHOLDER_FILLER）: 見出し語・透かし語と
#    助詞・記号を除いて何も残らない（「得意先の会社名」「株式会社」「見本」「テスト株式会社」）。
#    例示値は「枠の下の span を読み順で連結したもの」（D11）なので、枠が見出しに掛かると
#    「会社名 株式会社山田」「TEL 03-1234-5678」のように**見出し語＋値**になる。値が残る
#    ものは通す（見出し語だけで落とすと正しいテンプレートのヒントを失う）。**数字は消さない**
#    （消すと「TEL 03-…」が空になる）。見出し語＋連番 1 文字（「担当者名1」「商品A」。ASCII
#    に限る。「氏名 李」の 1 字姓は値）は記入欄の連番なので落とす
# 3. 伏せ字だけ（_MASK_GLYPHS）: 法人格・敬称・住所の単位字・元号と年月日・通貨・数字・
#    区切りを剥がした残りが伏せ字（〇○●×＊□△…）だけ（「〇〇株式会社」「〒○○○-○○○○」
#    「令和○年○月○日」「○○県○○市○○町1-2-3」）。「高田馬場XX-X-XX」「○-○-○○○生命ビル」
#    「東京都○○区○○1-2-3」のように固有の実文字が残るものは値（紙面にそう印字されている）。
#    X は英字と区別できないので、**残りが X と伏せ字だけ**（「03-XXXX-XXXX」「20XX年XX月XX日」）
#    のときだけ伏せ字に数える
# 4. ゼロ埋め: 数字が 4 桁以上あってすべて 0（「〒000-0000」「¥000,000」「0000年00月00日」）。
#    「03-0000-0000」は市外局番が実数字なので通す（電話番号の形式知識までは持たない）
#
# 採らないと決めたもの（第 5 回の敵対的検証で「実在しうる」と判断）: 「ロゴ」（株式会社
# ロゴスコーポレーション）、「商事」「ABC」「XYZ」「Acme」（ABC-MART・Acme Markets）、
# 「123 Main St」、「hoge/foo」、「N/A」「-」「No.」（実文書の「該当なし」）、「$0.00」。
_PLACEHOLDER_STRONG = re.compile(
    # 住所1 / 住所 2（連番付きの記入欄）。「住所 1-2-3」「住所 1丁目」は見出し＋番地始まりの値
    r"住所\s*[1-9](?![\d\-−丁番号])"
    r"|(?:ビル名|建物名|部屋番号|号室|階数)\s*(?:等|など)"
    r"|都道府県|市区町村"  # 実住所には現れない（住所欄の説明）
    # 「ロゴ」は採らない（株式会社ロゴスコーポレーションのような実社名がある）。
    # sample13 の「ロゴや社判も登録できます」は「登録できます」「社判」で落ちる
    # 「社判」は「判治」「判田」（実在する姓: 株式会社判治商店）を巻き込まないよう「会社判」を除く
    r"|(?<!会)社判|登録できます|入力してください|ご?記入ください|選択してください"
    r"|を入力|を記入|ご?記入例|入力例|記載例|記入欄|入力欄|記載欄|自由記入"
    r"|サンプルテキスト|ダミーテキスト|テキストを入力"
    r"|^ここに|^例\s*[)）:：]"  # 「例）株式会社〇〇」「例：山田太郎」
    r"|※\s*必須|必須項目"
    r"|^(?:令和|平成|昭和|西暦)?\s*年\s*月\s*日$"  # 白紙の日付枠
    # 書式指定。英語の語は語境界を見る（「Prototype Here」「Data Center The Hub」を巻き込まない）
    r"|(?<![A-Za-z])(?:YYYY|MM/DD|DD/MM)(?![A-Za-z])|(?<![A-Za-z0-9])(?:MM月|DD日)"
    r"|\baddress\s*line\s*[1-9]\b|\blorem\s*ipsum\b|\byour\s+(?:company|name|address|logo)\b|\blogo\s+here\b"
    r"|\bcompany\s+name\s+here\b|\btype\s+here\b|\bclick\s+here\b|\benter\s+(?:your|the|text)\b"
    r"|\bchoose\s+an\s+item\b|\bplaceholder\s+text\b|\bsample\s+text\b|\bdummy\s+text\b|\banytown\b"
    r"|\binsert\s+(?:name|your|company|text|logo|date|here)\b|^\[[^\[\]]+\]$"  # [Insert name]
    r"|\{\{.*\}\}|\$\{.*\}"  # 差し込み変数 {{customer.name}} ${InvoiceNumber}
    r"|@example\.|\bexample\.(?:com|net|org|jp|co\.jp)\b|^sample@"  # RFC 2606 の予約ドメイン
    r"|\(?\b555\)?[\s\-]?555[\s\-]?\d{4}\b"  # 北米の架空番号帯
    r"|^(?:tbd|tba|n/?a)$|^(?:john|jane)\s+doe$"
    r"|firmenname|musterfirma|mustermann|musterfrau|musterstadt|musterstra(?:ß|ss)e"
    r"|nombre\s+de\s+la\s+empresa|公司名称|회사명",
    re.IGNORECASE,
)
# 見出し語・透かし語。長い語から当てる（「会社名称」を「会社名」で切ると「称」が残る）ので
# 長さ順に並べてから正規表現にする
_PLACEHOLDER_LABEL_WORDS = (
    "自社名", "貴社名", "御社名", "会社名称", "会社名", "社名", "名称",
    "得意先名", "取引先名", "顧客名", "お客様名", "お客様", "得意先", "取引先", "顧客",
    "宛名", "氏名", "フリガナ", "ふりがな", "お名前", "名前", "担当者名", "担当者", "担当",
    # 役職語（代表取締役）は入れない: 「役職 代表取締役」は見出し＋値（役職の項目の値）
    "部署名", "部署", "役職", "御中", "様", "殿", "印",
    "住所", "所在地", "ビル名", "建物名", "部屋番号", "電話番号", "電話", "FAX番号", "郵便番号", "〒", "TEL", "FAX",
    "メールアドレス", "メール", "請求先", "納品先", "送付先", "支払先", "宛先", "発行元", "発行者",
    "件名", "品名", "商品名", "商品", "品目", "金額", "合計", "小計", "消費税", "税込", "税抜",
    "日付", "番号", "会社", "貴社", "御社", "自社", "名",
    "株式会社", "有限会社", "合同会社", "合資会社", "合名会社", "(株)", "(有)",
    # 透かし語・見本語・汎用のダミー名（残りに実体が無ければ記入例。「サンプル商事株式会社」は
    # 商事が、「テストー株式会社」（testo の日本法人）は長音が残るので値）。英語の法人格
    # （Inc / Ltd）は入れない: 正解値に「sample.Inc」（sample30）がある
    "サンプル", "見本", "ダミー", "テスト", "sample", "dummy", "draft", "太郎", "花子", "一郎", "次郎", "三郎",
    r"company\s*name", r"client\s*name", r"customer\s*name", r"contact\s*name", r"street\s*address",
    r"zip\s*code", r"bill\s*to", r"ship\s*to", r"sold\s*to", "attn",
    "address", "company", "customer", "client", "contact", "invoice", "number", "amount", "total",
    "signature", "title", "logo", "name", "city", "state", "zip", "code", r"\bst\b", "phone", "tel", "fax",
    r"e\-?mail", "date",
)
_PLACEHOLDER_LABELS = re.compile(
    "|".join(
        w if "\\" in w else re.escape(w)
        for w in sorted(_PLACEHOLDER_LABEL_WORDS, key=len, reverse=True)
    ),
    re.IGNORECASE,
)
# 見出し語を除いた残りから消す: 助詞・つなぎ・括弧・区切り・空白。**数字は消さない**
# （「TEL 03-1234-5678」は見出し＋値。数字まで消すと空になって落ちる）
_PLACEHOLDER_FILLER = re.compile(
    r"[のやもとにをはがで等々]|など|ご|お"
    # 長音「ー」は消さない（「テストー株式会社」を見出し語だけにしないため）
    r"|[\s()（）\[\]{}「」『』【】<>《》:：;；,，、.。・/／\-−‐‑–—_＿*＊+＋=＝|｜~〜～\"'“”‘’!！?？&＆#＃%％@]"
)
_MASK_GLYPHS = frozenset("〇○◯●◎××✕✗＊*□■◇◆△▲▽▼☐☑_＿")
_MASK_X = frozenset("Xx")
# 伏せ字判定の前に剥がすもの: 法人格・敬称・役職・住所の単位字・建物・元号と年月日・通貨・
# 数字・区切り・URL の骨格・口座名義のカナ略称・汎用のダミー名（太郎・花子）
_MASK_STRIP = re.compile(
    r"株式会社|有限会社|合同会社|合資会社|合名会社|\(株\)|\(有\)|㈱|㈲|御中|様|殿|印"
    r"|代表取締役|取締役|代表者|担当"
    r"|都|道|府|県|市|区|町|村|郡|丁目|番地|番|号|〒"
    r"|ビル|マンション|\d+\s*F|階"
    r"|令和|平成|昭和|大正|西暦|年|月|日|¥|￥|円|\$"
    r"|商事|商会|商店|工業|製作所|物産|建設|興業|産業|運輸|不動産|銀行|支店|普通|当座|一式|作業"
    r"|https?://|www\.|\.co\.jp|\.com|\.jp|\.net|\.org"
    r"|カ\)|ユ\)|ド\)|太郎|花子|一郎|次郎|三郎"
    r"|[0-9]|[\s()（）\[\]「」【】,，、.。・/／\-−‐‑–—ー:：;；]",
)
_ZERO_FILL_MIN_DIGITS = 4
# 会計の負数「△12,000」「▲1,250,000」「¥▲1,000」の △▲ は伏せ字ではない
_NEG_AMOUNT_SIGN = re.compile(r"^[¥￥$]?\s*[△▲]\s*(?=[¥￥$]?\d)")
# 数量の「×2」「2×3」の × は伏せ字ではない
_QUANTITY_X = re.compile(r"(?:^|\d)\s*×\s*\d|\d\s*×\s*$")
# マスク済みの番号「****1234」の * は伏せ字ではない（数字が伴うとき）
_MASKED_NUMBER = re.compile(r"[*＊]+[\s\-−]*\d|\d[\s\-−]*[*＊]+")
# 時刻「00:00」はゼロ埋めではない
_TIME_LIKE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def is_placeholder_example(example_value: Optional[str]) -> bool:
    """例示値が記入例語（値ではなく記入欄の説明）か。"""
    if not example_value:
        return False
    t = unicodedata.normalize("NFKC", str(example_value)).strip()
    if not t:
        return False
    # 1. 強い印
    if _PLACEHOLDER_STRONG.search(t):
        return True
    # 2. 見出し語だけ。見出し語が 1 つも無いもの（「395,217」「No.」）は記号を消しても
    #    記入例語ではない。見出し語＋連番 1 文字（「担当者名1」「商品A」）は記入欄の連番。
    #    0 は連番に無い（「消費税 0」「合計 0」は非課税明細の実値）
    labels_removed, n_labels = _PLACEHOLDER_LABELS.subn("", t)
    rest = _PLACEHOLDER_FILLER.sub("", labels_removed)
    if n_labels and (not rest or (len(rest) == 1 and rest.isascii() and rest.isalnum() and rest != "0")):
        return True
    # 3. 伏せ字だけ。見出し語を除いた後にも当てる（「会社名 〇〇株式会社」「TEL ○○-○○○○」）。
    #    X は残りが X と伏せ字だけのときに限って数える。数量の ×・マスク済み番号の * は除く
    if not _QUANTITY_X.search(t) and not _MASKED_NUMBER.search(t):
        for base in (t, labels_removed):
            masked = _MASK_STRIP.sub("", _NEG_AMOUNT_SIGN.sub("", base))
            if masked and all(ch in _MASK_GLYPHS for ch in masked):
                return True
            if len(masked) >= 2 and all(ch in _MASK_GLYPHS or ch in _MASK_X for ch in masked):
                return True
    # 4. ゼロ埋め（時刻「00:00」は除く）
    if _TIME_LIKE.match(t):
        return False
    digits = [ch for ch in t if ch.isdigit()]
    return len(digits) >= _ZERO_FILL_MIN_DIGITS and all(ch == "0" for ch in digits)
