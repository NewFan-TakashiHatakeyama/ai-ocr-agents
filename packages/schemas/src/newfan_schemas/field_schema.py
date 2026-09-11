"""抽出スキーマ定義（詳細設計 §5.5）。field_schemas.fields の形式。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from newfan_schemas.enums import FieldType


# 正規化矩形の最小面積。クリック誤検出（1px ドラッグ）で潰れた矩形が保存され、
# 以降のフィルタで予期しない挙動になるのを防ぐ。ページ全体の 0.01%。
MIN_REGION_AREA = 0.0001

# 例示値（RegionRect.example_value）の上限文字数。LLM プロンプトに入る文字列なので
# 無制限にしない。kie.py が自動発見時の label に課す 120 字と同じ考え方。
EXAMPLE_VALUE_MAX_LEN = 200


# ---- 予約された項目名（設計 region-field-add-and-hint-v2 §1.1 D9） ----
#
# confidence_gate は「特定の項目に紐付かない所見」（読み取れなかったページ・除外領域
# の集約所見）を、擬似的な field_name を持つ ReviewItem として積む。スキーマの項目が
# 同じ名前を持つと、検証画面でその項目の所見と集約所見が区別できなくなる。
# 先頭 ``__`` は将来の集約名のために丸ごと予約する。
# 定義をここ（newfan_schemas）に置くのは、orchestrator（積む側）と gateway
# （項目名を検査する側）の両方が同じ定数を見るため。二重定義にしない。
LOST_PAGE_FIELD = "__pages__"
REGION_AGGREGATE_FIELD = "__region__"
REVIEW_AGGREGATE_FIELD_NAMES: frozenset[str] = frozenset(
    {LOST_PAGE_FIELD, REGION_AGGREGATE_FIELD}
)
RESERVED_FIELD_NAME_PREFIX = "__"


def check_field_name(name: str) -> None:
    """スキーマ項目名として使えない予約名なら ValueError（D9）。

    UI の命名規則（英字始まり）は通さないが、チャット経路は任意の名前を受けるので、
    経路によらずサーバ側で拒む。``FieldDef`` / gateway の ``SchemaFieldDef`` の
    validator と、``PUT /schemas`` の明示検査（API のエラー封筒で返すため）が呼ぶ。
    """
    if name in REVIEW_AGGREGATE_FIELD_NAMES or name.startswith(RESERVED_FIELD_NAME_PREFIX):
        raise ValueError(
            f"項目名「{name}」は予約されています"
            f"（先頭が {RESERVED_FIELD_NAME_PREFIX} の名前はレビュー所見の集約名に使うため指定できません）"
        )


class RegionRect(BaseModel):
    """スキーマに保存する領域（設計 §4.1）。

    ランタイムの ``bbox``（Span / ExtractedField / TableCell）は**前処理後 PNG の
    画素 int** だが、こちらは**当該ページ寸法に対する正規化 [0,1] float** である。
    キー名を ``rect`` と分けているのは、両者が混ざると「どちらの座標系か」を
    型では判別できず、画素値を正規化矩形として保存する事故が静かに通るため。

    page:
      - int（1 始まり）: そのページだけ
      - "last": ページ数可変帳票の最終ページ（承認印・合計欄）
      - None: 全ページ。**exclude でのみ許可**（include に許すと「どこを読むか」
        の指定にならない）。文脈依存の制約なので gateway の put_schema で検査する。

    example_value / origin / created_at（設計 region-field-add-and-hint-v2 §2.3）:
      **読取領域のみ。除外領域では未使用**（付いていても無害で、検証は通る）。
      - example_value: テンプレート元の帳票でこの領域にあった span の原文。KIE に
        「前回ここには何があったか」を伝える。正規化しない。**帳票の値（取引先名・
        担当者の個人名を含み得る）であり、スキーマの版が残る限り残る**
        （``put_schema`` は常に新版 INSERT で旧版を残す。``DELETE /documents`` は
        帳票を消してもこれは消さない）。LLM プロンプトに入るので、印字可能文字のみ・
        前後空白除去・200 字で打ち切る（kie.py が label に課すのと同じ規則）。
      - origin: 領域の出どころ。``ghost`` = AI が見つけた位置をクリックで採った /
        ``manual`` = 手描き。例示値の信頼度と計測の切り口が変わる。
      - created_at: ISO 8601。ヒント有効化前に引かれた領域を識別する。
      fields JSONB の中なのでマイグレーション不要。既存の領域は 3 つとも None。
    """

    page: Optional[Union[int, Literal["last"]]] = None
    rect: list[float]  # [x1, y1, x2, y2] 正規化 0..1
    label: Optional[str] = None  # exclude の表示名（「社印」等）。include では未使用
    # --- 以下、読取領域のみ（除外領域では未使用） ---
    example_value: Optional[str] = None
    origin: Optional[Literal["ghost", "manual"]] = None
    created_at: Optional[str] = None

    @field_validator("example_value")
    @classmethod
    def _sanitize_example_value(cls, v: Optional[str]) -> Optional[str]:
        # 制御文字（U+0000 は Pg の TEXT に入らず保存ごと落ちる）・改行を落とし、
        # 前後の空白を除いて上限で切る。残らなければ「例示値なし」と同じ None。
        if v is None:
            return None
        cleaned = "".join(ch for ch in v if ch.isprintable())
        return cleaned.strip()[:EXAMPLE_VALUE_MAX_LEN] or None

    @field_validator("created_at")
    @classmethod
    def _created_at_iso8601(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        # JS の Date.toISOString() は末尾 Z。Python 3.12 の fromisoformat は Z を
        # 受けるが、規約として +00:00 に読み替えてから解釈する（版差で揺れない）。
        probe = v[:-1] + "+00:00" if v.endswith("Z") else v
        try:
            datetime.fromisoformat(probe)
        except ValueError as exc:
            raise ValueError(f"created_at は ISO 8601 形式で指定してください: {v!r}") from exc
        return v

    @field_validator("page")
    @classmethod
    def _page_positive(
        cls, v: Optional[Union[int, str]]
    ) -> Optional[Union[int, str]]:
        # bool は int のサブクラスなので明示的に弾く（page=True が 1 として通る）
        if isinstance(v, bool):
            raise ValueError("page には bool を指定できません")
        if isinstance(v, int) and v < 1:
            raise ValueError('page は 1 以上の整数、"last"、または null です')
        return v

    @model_validator(mode="after")
    def _check_rect(self) -> "RegionRect":
        r = self.rect
        if len(r) != 4:
            raise ValueError("rect は [x1, y1, x2, y2] の 4 要素です")
        if not all(0.0 <= float(v) <= 1.0 for v in r):
            raise ValueError("rect の各値は 0..1 の正規化座標です")
        x1, y1, x2, y2 = (float(v) for v in r)
        if x1 >= x2 or y1 >= y2:
            raise ValueError("rect は x1 < x2 かつ y1 < y2 である必要があります")
        if (x2 - x1) * (y2 - y1) <= MIN_REGION_AREA:
            raise ValueError("rect の面積が小さすぎます（誤クリック由来の矩形の可能性）")
        self.rect = [x1, y1, x2, y2]
        return self



def resolve_page(
    page: Optional[Union[int, str]], page_no: int, page_count: int
) -> bool:
    """RegionRect.page が当該ページに適用されるか（設計 §5.1）。

    orchestrator（除外の適用）と gateway（検証画面へ返す解決済み領域）の**両方**が
    同じ規則で解決する必要があるため、ここに置いて共有する。web に再実装させると
    「最終ページ限定の承認印除外が全ページに描かれる／描かれない」事故になる。

    - None: 全ページ
    - "last": ページ数可変帳票の最終ページ
    - int: そのページ。**run の総ページ数を超える指定は適用しない**
      （2 ページ帳票で作った p2 の領域を 1 ページ帳票へ当てない。1 ページ目への
      縮退は誤った位置を決定論削除するので採らない）
    """
    if page is None:
        return True
    if page == "last":
        return page_count >= 1 and page_no == page_count
    if isinstance(page, int) and not isinstance(page, bool):
        return 1 <= page <= page_count and page == page_no
    return False


def resolve_regions(
    regions: list[Any], page_count: int
) -> list[dict[str, Any]]:
    """RegionRect 列を ``{page_no, rect, label}`` へ展開する（設計 §6）。

    ``"last"`` と ``None`` をサーバ側で解決してから返すことで、受け手（検証画面）は
    ページ番号の一致だけを見ればよくなる。dict / RegionRect のどちらでも受ける
    （state 経由は JSONB 由来の dict、gateway 経由はモデル）。
    """
    out: list[dict[str, Any]] = []
    for r in regions:
        page = r.get("page") if isinstance(r, dict) else getattr(r, "page", None)
        rect = r.get("rect") if isinstance(r, dict) else getattr(r, "rect", None)
        label = r.get("label") if isinstance(r, dict) else getattr(r, "label", None)
        if not rect or len(rect) < 4:
            continue
        for page_no in range(1, page_count + 1):
            if resolve_page(page, page_no, page_count):
                out.append({"page_no": page_no, "rect": list(rect), "label": label})
    return out


class ColumnDef(BaseModel):
    name: str
    type: FieldType = FieldType.STRING
    label: Optional[str] = None


class FieldDef(BaseModel):
    name: str
    label: Optional[str] = None
    type: FieldType = FieldType.STRING
    required: bool = False
    critical: bool = False
    columns: Optional[list[ColumnDef]] = None
    # 読み取ってほしい領域（設計 §4.2）。**hint であって hard crop ではない**ため、
    # region の外で見つかった値を捨てる根拠にはしない。
    region: Optional[RegionRect] = None

    @field_validator("name")
    @classmethod
    def _name_not_reserved(cls, v: str) -> str:
        check_field_name(v)  # D9: 集約 ReviewItem の擬似 field 名と衝突させない
        return v


class FieldSchema(BaseModel):
    doc_type: str
    fields: list[FieldDef] = Field(default_factory=list)

    def critical_field_names(self) -> set[str]:
        return {f.name for f in self.fields if f.critical}
