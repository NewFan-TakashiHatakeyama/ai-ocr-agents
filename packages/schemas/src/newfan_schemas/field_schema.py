"""抽出スキーマ定義（詳細設計 §5.5）。field_schemas.fields の形式。"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from newfan_schemas.enums import FieldType


# 正規化矩形の最小面積。クリック誤検出（1px ドラッグ）で潰れた矩形が保存され、
# 以降のフィルタで予期しない挙動になるのを防ぐ。ページ全体の 0.01%。
MIN_REGION_AREA = 0.0001


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
    """

    page: Optional[Union[int, Literal["last"]]] = None
    rect: list[float]  # [x1, y1, x2, y2] 正規化 0..1
    label: Optional[str] = None  # exclude の表示名（「社印」等）。include では未使用

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


class FieldSchema(BaseModel):
    doc_type: str
    fields: list[FieldDef] = Field(default_factory=list)

    def critical_field_names(self) -> set[str]:
        return {f.name for f in self.fields if f.critical}
